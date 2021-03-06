import os
import random
import logging
from pathlib import Path
from typing import List, Tuple, Iterable, Union, Sequence, Dict
from dataclasses import dataclass, field, asdict

import numpy as np
import torch
from torch.nn.utils.clip_grad import clip_grad_norm_
from tqdm import tqdm

from .logger import Logger

try:
    from apex import amp
    APEX_AVAILABLE = True
except ModuleNotFoundError:
    APEX_AVAILABLE = False

SEED = int(os.environ.get("SEED", 9293))

random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed(SEED)
torch.backends.cudnn.benchmark = True

if os.environ.get("DETERMINISTIC", None):
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


class StopTraining(Exception):
    pass


def batch_to_device(batch, device):
    results = []
    if isinstance(batch, dict):
        return batch_to_device([batch], device)
    for item in batch:
        if isinstance(item, dict):
            for key in item:
                item[key] = item[key].to(device)
            results.append(item)
        elif isinstance(item, tuple):
            results.append(tuple(
                x.to(device) for x in item
            ))
        else:
            results.append(item.to(device))
    return results


def get_batch_size(batch, batch_dim):
    if isinstance(batch[0], dict):
        for key in batch[0]:
            return batch[0][key].size(batch_dim)
    elif isinstance(batch[0], torch.Tensor):
        return batch[0].size(batch_dim)
    else:
        return len(batch[0])


def concatenate_batches(batches):
    if isinstance(batches[0], dict):
        results = {}
        for key in batches[0].keys():
            results[key] = torch.cat([
                x[key] for x in batches
            ])
        return results
    return torch.cat(batches)


@dataclass
class BaseBot:
    """Base Interface to Model Training and Inference"""
    train_loader: Iterable
    valid_loader: Iterable
    criterion: object
    model: torch.nn.Module
    optimizer: torch.optim.Optimizer
    name: str = "basebot"
    use_amp: Union[bool, str] = False
    clip_grad: float = 0
    batch_dim: int = 0
    device: Union[str, torch.device] = "cuda:0"
    log_dir: Path = Path("/tmp/pytorch_helper_bot_logs/")
    log_level: int = logging.INFO
    loss_format: str = "%.8f"
    use_tensorboard: bool = False
    gradient_accumulation_steps: int = 1
    echo: bool = True
    step: int = 0
    total_steps: int = 0
    metrics: Sequence = ()
    callbacks: Sequence = ()
    pbar: bool = False
    expand_dict_inputs: bool = True

    def __post_init__(self):
        assert (self.use_amp and APEX_AVAILABLE) or (not self.use_amp)
        self.logger = Logger(
            self.name, str(self.log_dir), self.log_level,
            use_tensorboard=self.use_tensorboard, echo=self.echo)
        self.logger.info("SEED: %s", SEED)
        self.count_model_parameters()
        if APEX_AVAILABLE:
            if not self.use_amp and (hasattr(amp._amp_state, "opt_properties")):
                self.logger.warning(
                    "AMP initialization detected but use_amp = False. "
                    "Did you forget to set `use_amp = True`?")

    def count_model_parameters(self):
        self.logger.info(
            "# of parameters: {:,d}".format(
                np.sum(list(p.numel() for p in self.model.parameters()))))
        self.logger.info(
            "# of trainable parameters: {:,d}".format(
                np.sum(list(p.numel() for p in self.model.parameters() if p.requires_grad))))

    def train_one_step(self, input_tensors, target):
        self.model.train()
        assert self.model.training
        if len(input_tensors) == 1 and isinstance(input_tensors[0], dict):
            if self.expand_dict_inputs:
                output = self.model(**input_tensors[0])
            else:
                output = self.model(input_tensors[0])
        else:
            output = self.model(*input_tensors)
        batch_loss = self.criterion(
            self.extract_prediction(output), target
        ) / self.gradient_accumulation_steps
        if torch.isnan(batch_loss):
            self.logger.warning("NAN Loss dectected! Skipping this step...")
        else:
            if self.use_amp:
                with amp.scale_loss(
                    batch_loss, self.optimizer,
                    delay_unscale=self.step % self.gradient_accumulation_steps != 0
                ) as scaled_loss:
                    scaled_loss.backward()
            else:
                batch_loss.backward()
            if self.step % self.gradient_accumulation_steps == 0:
                if self.clip_grad > 0:
                    if not self.use_amp:
                        for param_group in self.optimizer.param_groups:
                            clip_grad_norm_(
                                param_group["params"], self.clip_grad)
                    else:
                        clip_grad_norm_(amp.master_params(
                            self.optimizer), self.clip_grad)
                self.optimizer.step()
                self.optimizer.zero_grad()
        return (
            batch_loss.data.cpu().item() * self.gradient_accumulation_steps,
            get_batch_size(input_tensors, self.batch_dim)
        )

    @staticmethod
    def extract_prediction(output):
        """Assumes multiple outputs"""
        return output

    def run_batch_inputs_callbacks(self, input_tensors, targets, is_eval):
        for callback in self.callbacks:
            input_tensors, targets = callback.on_batch_inputs(
                self, input_tensors, targets, is_eval)
        return input_tensors, targets

    def run_step_ends_callbacks(self, train_loss, train_weight):
        for callback in self.callbacks:
            callback.on_step_ends(self, train_loss, train_weight)

    def run_train_starts_callbacks(self):
        for callback in self.callbacks:
            callback.on_train_starts(self)

    def run_train_ends_callbacks(self):
        for callback in self.callbacks:
            callback.on_train_ends(self)

    def run_epoch_ends_callbacks(self, epoch):
        for callback in self.callbacks:
            callback.on_epoch_ends(self, epoch)

    def run_eval_starts_callbacks(self):
        for callback in self.callbacks:
            callback.on_eval_starts(self)

    def run_eval_ends_callbacks(self, metrics):
        for callback in self.callbacks:
            callback.on_eval_ends(self, metrics)

    def train(self, *, checkpoint_interval, n_steps=None, total_steps=None):
        if total_steps:
            self.total_steps = total_steps
        if n_steps is None:
            if self.total_steps is None:
                raise ValueError("n_steps and total_steps cannot both be None")
            n_steps = self.total_steps - self.step
        elif total_steps is None:
            self.total_steps = n_steps
        assert self.total_steps != 0, f"{total_steps}, {n_steps}"
        target_step = self.step + n_steps
        self.optimizer.zero_grad()
        epoch = 0
        self.logger.info(
            "Optimizer {}".format(str(self.optimizer)))
        try:
            self.logger.info("Batches per epoch: {}".format(
                len(self.train_loader)))
        except TypeError:
            # IterableDataset doesn't have length
            pass
        # Train starts
        self.run_train_starts_callbacks()
        try:
            while self.step < target_step:
                epoch += 1
                self.logger.info(
                    "=" * 20 + "Epoch %d" + "=" * 20, epoch)
                for *input_tensors, targets in self.train_loader:
                    input_tensors, targets = self.run_batch_inputs_callbacks(
                        input_tensors, targets, is_eval=False)
                    input_tensors = batch_to_device(input_tensors, self.device)
                    targets = batch_to_device([targets], self.device)[0]
                    self.step += 1
                    train_loss, train_weight = self.train_one_step(
                        input_tensors, targets)
                    # Step ends
                    self.run_step_ends_callbacks(train_loss, train_weight)
                    if (
                        (callable(checkpoint_interval) and checkpoint_interval(self.step)) or
                        (not callable(checkpoint_interval) and
                         self.step % checkpoint_interval == 0)
                    ):
                        # Eval starts
                        self.run_eval_starts_callbacks()
                        metrics = self.eval(self.valid_loader)
                        # Eval ends
                        self.run_eval_ends_callbacks(metrics)
                    if self.step >= target_step:
                        break
                # Epoch ends
                self.run_epoch_ends_callbacks(epoch + 1)
        except (KeyboardInterrupt, StopTraining):
            pass
        finally:
            # Train ends
            self.run_train_ends_callbacks()

    def eval(self, loader):
        """Warning: Only support datasets whose predictions and labels together fit in memory."""
        self.model.eval()
        preds, ys = [], []
        losses, weights = [], []
        self.logger.debug("Evaluating...")
        with torch.no_grad():
            for *input_tensors, y_local in tqdm(loader, disable=not self.pbar, ncols=100):
                input_tensors, y_local = self.run_batch_inputs_callbacks(
                    input_tensors, y_local, is_eval=True)
                input_tensors = batch_to_device(input_tensors, self.device)
                y_local = batch_to_device([y_local], self.device)[0]
                if len(input_tensors) == 1 and isinstance(input_tensors[0], dict):
                    if self.expand_dict_inputs:
                        output = self.extract_prediction(
                            self.model(**input_tensors[0]))
                    else:
                        output = self.extract_prediction(
                            self.model(input_tensors[0]))
                else:
                    output = self.extract_prediction(
                        self.model(*input_tensors))
                batch_loss = self.criterion(output, y_local)
                losses.append(batch_loss.data.cpu().item())
                weights.append(output.size(self.batch_dim))
                # Save batch labels and predictions
                preds.append(output.cpu())
                ys.append(batch_to_device([y_local], "cpu")[0])
        loss = np.average(losses, weights=weights)
        metrics = {"loss": (loss, self.loss_format % loss)}
        global_ys, global_preds = (
            concatenate_batches(ys), torch.cat(preds).float()
        )
        for metric in self.metrics:
            metric_loss, metric_string = metric(global_ys, global_preds)
            metrics[metric.name] = (metric_loss, metric_string)
        return metrics

    def predict_batch(self, input_tensors):
        self.model.eval()
        if len(input_tensors) == 1 and isinstance(input_tensors[0], dict):
            if self.expand_dict_inputs:
                tmp = self.extract_prediction(
                    self.model(**input_tensors[0]))
            else:
                tmp = self.extract_prediction(
                    self.model(input_tensors[0]))
        else:
            tmp = self.model(*input_tensors)
        return self.extract_prediction(tmp)

    def predict(self, loader, *, return_y=False):
        self.model.eval()
        outputs, y_global = [], []
        with torch.no_grad():
            for *input_tensors, y_local in tqdm(loader, disable=not self.pbar, ncols=100):
                input_tensors = batch_to_device(input_tensors, self.device)
                outputs.append(self.predict_batch(input_tensors).cpu())
                if return_y:
                    y_global.append(y_local)
            outputs = torch.cat(outputs, dim=0)
        if return_y:
            y_global = torch.cat(y_global, dim=0)
            return outputs, y_global.cpu()
        return outputs

    def load_model(self, target_path):
        self.model.load_state_dict(torch.load(
            target_path, map_location="cpu")["model"])

    def state_dict(self):
        """States needed to resume training from this point"""
        with torch.no_grad():
            # Do not copy these two (to save memory)
            model, self.model = self.model, None
            optimizer, self.optimizer = self.optimizer, None
            # drop loader to potentially save disk space
            train_loader, self.train_loader = self.train_loader, None
            valid_loader, self.valid_loader = self.valid_loader, None
            # Avoid copying optimizers
            for callback in self.callbacks:
                callback.on_save_checkpoint()
            # Uncomment to show debug messages:
            # from dataclasses import fields
            # for field in fields(self):
            #     print(field.name)
            #     print(getattr(self, field.name))
            state_dict = asdict(self)
            state_dict["model"] = model.state_dict()
            state_dict["optimizer"] = optimizer.state_dict()
            if self.use_amp:
                state_dict["amp"] = amp.state_dict()
            # Restoring stuffs
            for callback in self.callbacks:
                callback.on_load_checkpoint(optimizer=optimizer, cold_start=False)
            self.model = model
            self.optimizer = optimizer
            self.train_loader = train_loader
            self.valid_loader = valid_loader
            return state_dict

    @classmethod
    def load_checkpoint(cls, ckpt_path, train_loader, valid_loader, model, optimizer):
        state_dict = torch.load(ckpt_path)
        state_dict["train_loader"] = train_loader
        state_dict["valid_loader"] = valid_loader
        optimizer.load_state_dict(state_dict["optimizer"])
        state_dict["optimizer"] = optimizer
        model.load_state_dict(state_dict["model"])
        state_dict["model"] = model
        for callback in state_dict["callbacks"]:
            callback.on_load_checkpoint(optimizer=state_dict["optimizer"], cold_start=True)
        if "amp" in state_dict:
            if APEX_AVAILABLE:
                amp.load_state_dict(state_dict["amp"])
                assert state_dict["use_amp"]
            del state_dict["amp"]
        return cls(**state_dict)


class DeepSpeedBot(BaseBot):
    def train_one_step(self, input_tensors, target):
        self.model.train()
        assert self.model.training
        if len(input_tensors) == 1 and isinstance(input_tensors[0], dict):
            if self.expand_dict_inputs:
                output = self.model(**input_tensors[0])
            else:
                output = self.model(input_tensors[0])
        else:
            output = self.model(*input_tensors)
        batch_loss = self.criterion(
            self.extract_prediction(output), target
        )  # / self.gradient_accumulation_steps
        if torch.isnan(batch_loss):
            self.logger.warning("NAN Loss dectected! Skipping this step...")
        else:
            self.model.backward(batch_loss)
            self.model.step()
        return (
            batch_loss.data.cpu().item() * self.gradient_accumulation_steps,
            get_batch_size(input_tensors, self.batch_dim)
        )

    def state_dict(self):
        raise NotImplementedError()

    def load_state_dict(self):
        raise NotImplementedError()
