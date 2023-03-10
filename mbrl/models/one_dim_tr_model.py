# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import pathlib
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import torch

import mbrl.models.util as model_util
import mbrl.types
import mbrl.util.math

from .model import Ensemble, Model
from collections import deque
import torch.nn.utils as utils
from torch.distributions.normal import Normal


MODEL_LOG_FORMAT = [
    ("train_iteration", "I", "int"),
    ("epoch", "E", "int"),
    ("train_dataset_size", "TD", "int"),
    ("val_dataset_size", "VD", "int"),
    ("model_loss", "MLOSS", "float"),
    ("model_score", "MSCORE", "float"),
    ("model_val_score", "MVSCORE", "float"),
    ("model_best_val_score", "MBVSCORE", "float"),
]


class OneDTransitionRewardModel(Model):
    """Wrapper class for 1-D dynamics models.

    This model functions as a wrapper for another model to convert transition
    batches into 1-D transition reward models. It also provides
    data manipulations that are common when using dynamics models with 1-D observations
    and actions, so that users don't have to manipulate the underlying model's
    inputs and outputs directly (e.g., predicting delta observations, input
    normalization).

    The wrapper assumes that the wrapped model inputs/outputs will be consistent with

        [pred_obs_{t+1}, pred_rewards_{t+1} (optional)] = model([obs_t, action_t]).

    To use with :class:mbrl.models.ModelEnv`, the wrapped model must define methods
    ``reset_1d`` and ``sample_1d``.

    Args:
        model (:class:`mbrl.model.Model`): the model to wrap.
        target_is_delta (bool): if ``True``, the predicted observations will represent
            the difference respect to the input observations.
            That is, ignoring rewards, pred_obs_{t + 1} = obs_t + model([obs_t, act_t]).
            Defaults to ``True``. Can be deactivated per dimension using ``no_delta_list``.
        normalize (bool): if true, the wrapper will create a normalizer for model inputs,
            which will be used every time the model is called using the methods in this
            class. Assumes the given base model has an attributed ``in_size``.
            To update the normalizer statistics, the user needs to call
            :meth:`update_normalizer` before using the model. Defaults to ``False``.
        normalize_double_precision (bool): if ``True``, the normalizer will work with
            double precision.
        learned_rewards (bool): if ``True``, the wrapper considers the last output of the model
            to correspond to rewards predictions, and will use it to construct training
            targets for the model and when returning model predictions. Defaults to ``True``.
        obs_process_fn (callable, optional): if provided, observations will be passed through
            this function before being given to the model (and before the normalizer also).
            The processed observations should have the same dimensions as the original.
            Defaults to ``None``.
        no_delta_list (list(int), optional): if provided, represents a list of dimensions over
            which the model predicts the actual observation and not just a delta.
        num_elites (int, optional): if provided, only the best ``num_elites`` models according
            to validation score are used when calling :meth:`predict`. Defaults to
            ``None`` which means that all models will always be included in the elite set.
    """

    def __init__(
        self,
        model: Model,
        target_is_delta: bool = True,
        normalize: bool = False,
        normalize_double_precision: bool = False,
        learned_rewards: bool = True,
        obs_process_fn: Optional[mbrl.types.ObsProcessFnType] = None,
        no_delta_list: Optional[List[int]] = None,
        num_elites: Optional[int] = None,
    ):
        super().__init__(model.device)
        self.model = model
        self.input_normalizer: Optional[mbrl.util.math.Normalizer] = None
        if normalize:
            self.input_normalizer = mbrl.util.math.Normalizer(
                self.model.in_size,
                self.model.device,
                dtype=torch.double if normalize_double_precision else torch.float,
            )
        self.device = self.model.device
        self.learned_rewards = learned_rewards
        self.target_is_delta = target_is_delta
        self.no_delta_list = no_delta_list if no_delta_list else []
        self.obs_process_fn = obs_process_fn

        self.num_elites = num_elites
        if not num_elites and isinstance(self.model, Ensemble):
            self.num_elites = self.model.num_members

    def _get_model_input(
        self,
        obs: mbrl.types.TensorType,
        action: mbrl.types.TensorType,
    ) -> torch.Tensor:
        if self.obs_process_fn:
            obs = self.obs_process_fn(obs)
        obs = model_util.to_tensor(obs).to(self.device)
        action = model_util.to_tensor(action).to(self.device)
        model_in = torch.cat([obs, action], dim=obs.ndim - 1)
        if self.input_normalizer:
            # Normalizer lives on device
            model_in = self.input_normalizer.normalize(model_in).float().to(self.device)
        return model_in

    def _process_batch(
        self, batch: mbrl.types.TransitionBatch, _as_float: bool = False
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        obs, action, next_obs, reward, _ = batch.astuple()
        if self.target_is_delta:
            target_obs = next_obs - obs
            for dim in self.no_delta_list:
                target_obs[..., dim] = next_obs[..., dim]
        else:
            target_obs = next_obs
        target_obs = model_util.to_tensor(target_obs).to(self.device)

        model_in = self._get_model_input(obs, action)
        if self.learned_rewards:
            reward = model_util.to_tensor(reward).to(self.device).unsqueeze(reward.ndim)
            target = torch.cat([target_obs, reward], dim=obs.ndim - 1)
        else:
            target = target_obs
        return model_in.float(), target.float()

    def forward(self, x: torch.Tensor, *args, **kwargs) -> Tuple[torch.Tensor, ...]:
        """Calls forward method of base model with the given input and args."""
        return self.model.forward(x, *args, **kwargs)

    def update_normalizer(self, batch: mbrl.types.TransitionBatch):
        """Updates the normalizer statistics using the batch of transition data.

        The normalizer will compute mean and standard deviation the obs and action in
        the transition. If an observation processing function has been provided, it will
        be called on ``obs`` before updating the normalizer.

        Args:
            batch (:class:`mbrl.types.TransitionBatch`): The batch of transition data.
                Only obs and action will be used, since these are the inputs to the model.
        """
        if self.input_normalizer is None:
            return
        obs, action = batch.obs, batch.act
        if obs.ndim == 1:
            obs = obs[None, :]
            action = action[None, :]
        if self.obs_process_fn:
            obs = self.obs_process_fn(obs)
        model_in_np = np.concatenate([obs, action], axis=obs.ndim - 1)
        self.input_normalizer.update_stats(model_in_np)

    def loss(
        self,
        batch: mbrl.types.TransitionBatch,
        target: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, Dict[str, Any]]:
        """Computes the model loss over a batch of transitions.

        This method constructs input and targets from the information in the batch,
        then calls `self.model.loss()` on them and returns the value and the metadata
        as returned by the model.

        Args:
            batch (transition batch): a batch of transition to train the model.

        Returns:
            (tensor and optional dict): as returned by `model.loss().`
        """
        assert target is None
        model_in, target = self._process_batch(batch)
        return self.model.loss(model_in, target=target)

    def update(
        self,
        batch: mbrl.types.TransitionBatch,
        optimizer: torch.optim.Optimizer,
        target: Optional[torch.Tensor] = None,
        agent=None, env=None, termination_fn=None, coeff=None,
    ) -> Tuple[torch.Tensor, Dict[str, Any]]:
        """Updates the model given a batch of transitions and an optimizer.

        Args:
            batch (transition batch): a batch of transition to train the model.
            optimizer (torch optimizer): the optimizer to use to update the model.
            agent: model value loss.
            env: model value loss.
            termination_fn: termination function.

        Returns:
            (tensor and optional dict): as returned by `model.loss().`
        """
        assert target is None
        model_in, target = self._process_batch(batch)
        loss, meta = self.model.update(model_in, optimizer, target=target, agent=agent, env=env, coeff=coeff)
        loss_nll = loss.item()
        # Value model gradient
        reward_history = []
        log_probs = []
        obs = env.reset()
        obs = torch.from_numpy(obs).cpu().float()
        done = False
        while not done:
            actions = agent.act_dummy(obs.cpu().float())
            #with torch.no_grad():
            (
                next_observs,
                pred_rewards,
                pred_terminals,
                log_p,
            ) = self.sample_ours(
                actions,
                obs,
                deterministic=False,
            )
            with torch.no_grad():
                done = termination_fn(actions, next_observs)
                obs, reward = next_observs.detach(), pred_rewards.detach()
                reward_history.append(reward)
            log_probs.append(log_p)
        R = 0
    #    model_value_loss = []
        returns = deque()
        #gamma = 0.99
        gamma = 1.0
        for r in reward_history[::-1]:
            R = r + gamma * R
            returns.appendleft(R)
#        print("Train MV ", returns[0])
        returns = torch.tensor(returns).detach()
        if returns.dim() > 1:
            returns = (returns - returns.mean()) / (returns.std() + 1e-5)
        for log_prob, R in zip(log_probs, returns):
            #leng = len(reward_history)
            leng = 1.0
            loss += -log_prob.mean() * R.mean() / leng * coeff
        #    returns.appendleft(R)
     #   returns = torch.tensor(returns)
     #   if returns.dim() > 1:
     #       returns = (returns - returns.mean()) / (returns.std() + 1e-5)
  #      for log_prob, R in zip(log_probs, returns):
  #          loss += -log_prob.mean() * R.mean().detach() / len(reward_history) * coeff
           # model_value_loss.append(-log_prob * R.detach())
        #model_value_loss = torch.cat(model_value_loss).mean() * coeff
        #loss_total = loss + model_value_loss
        #returns.clear()
        #loss_total = loss
        optimizer.zero_grad()
        loss.backward()
        if meta is not None:
            with torch.no_grad():
                grad_norm = 0.0
                for p in list(filter(lambda p: p.grad is not None, self.parameters())):
                    grad_norm += p.grad.data.norm(2).item() ** 2
                meta["grad_norm"] = grad_norm
      #  utils.clip_grad_norm(self.model.parameters(), 40)
        optimizer.step()
        del reward_history
        del log_probs
        del returns
        loss_item = loss.item()
 #       print("NLL: ", loss_nll, "OPT: ", loss_item - loss_nll)
        del loss
        return loss_item, meta
        #return loss, meta

    def eval_score(
        self,
        batch: mbrl.types.TransitionBatch,
        target: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, Dict[str, Any]]:
        """Evaluates the model score over a batch of transitions.

        This method constructs input and targets from the information in the batch,
        then calls `self.model.eval_score()` on them and returns the value.

        Args:
            batch (transition batch): a batch of transition to train the model.

        Returns:
            (tensor): as returned by `model.eval_score().`
        """
        assert target is None
        with torch.no_grad():
            model_in, target = self._process_batch(batch)
            return self.model.eval_score(model_in, target=target)

    def get_output_and_targets(
        self, batch: mbrl.types.TransitionBatch
    ) -> Tuple[Tuple[torch.Tensor, ...], torch.Tensor]:
        """Returns the model output and the target tensors given a batch of transitions.

        This method constructs input and targets from the information in the batch,
        then calls `self.model.forward()` on them and returns the value.
        No gradient information will be kept.

        Args:
            batch (transition batch): a batch of transition to train the model.

        Returns:
            (tuple(tensor), tensor): the model outputs and the target for this batch.
        """
        with torch.no_grad():
            model_in, target = self._process_batch(batch)
            output = self.model.forward(model_in)
        return output, target

    def sample_ours(
        self,
        act: torch.Tensor,
        obs: torch.Tensor,
        deterministic: bool = False,
        rng: Optional[torch.Generator] = None,
    ) -> Tuple[
        torch.Tensor,
        Optional[torch.Tensor],
        Optional[torch.Tensor],
        Optional[torch.Tensor],
    ]:
        """Samples next observations and rewards from the underlying 1-D model.

        This wrapper assumes that the underlying model's sample method returns a tuple
        with just one tensor, which concatenates next_observation and reward.

        Args:
            act (tensor): the action at.
            model_state (tensor): the model state st.
            deterministic (bool): if ``True``, the model returns a deterministic
                "sample" (e.g., the mean prediction). Defaults to ``False``.
            rng (random number generator): a rng to use for sampling.

        Returns:
            (tuple of two tensors): predicted next_observation (o_{t+1}) and rewards (r_{t+1}).
        """
        obs = model_util.to_tensor(obs).to(self.device)
        model_in = self._get_model_input(obs, act)
        if not hasattr(self.model, "sample_1d"):
            raise RuntimeError(
                "OneDTransitionRewardModel requires wrapped model to define method sample_1d"
            )
        means, logvars = self.forward(
            model_in, rng=rng
        )
        variances = logvars.exp()
        stds = torch.sqrt(variances)
        gauss = Normal(means, stds)
        #sampled_s = means
        sampled_s = torch.normal(means, stds, generator=rng)
        log_prob = gauss.log_prob(sampled_s)
        preds, log_prob = sampled_s, log_prob

        next_observs = preds[:, :-1] if self.learned_rewards else preds
        if self.target_is_delta:
            tmp_ = next_observs + obs
            for dim in self.no_delta_list:
                tmp_[:, dim] = next_observs[:, dim]
            next_observs = tmp_
        rewards = preds[:, -1:] if self.learned_rewards else None
        return next_observs.detach(), rewards.detach(), None, log_prob

    def sample(
        self,
        act: torch.Tensor,
        model_state: Dict[str, torch.Tensor],
        deterministic: bool = False,
        rng: Optional[torch.Generator] = None,
    ) -> Tuple[
        torch.Tensor,
        Optional[torch.Tensor],
        Optional[torch.Tensor],
        Optional[Dict[str, torch.Tensor]],
        Optional[torch.Tensor],
    ]:
        """Samples next observations and rewards from the underlying 1-D model.

        This wrapper assumes that the underlying model's sample method returns a tuple
        with just one tensor, which concatenates next_observation and reward.

        Args:
            act (tensor): the action at.
            model_state (tensor): the model state st.
            deterministic (bool): if ``True``, the model returns a deterministic
                "sample" (e.g., the mean prediction). Defaults to ``False``.
            rng (random number generator): a rng to use for sampling.

        Returns:
            (tuple of two tensors): predicted next_observation (o_{t+1}) and rewards (r_{t+1}).
        """
        obs = model_util.to_tensor(model_state["obs"]).to(self.device)
        model_in = self._get_model_input(model_state["obs"], act)
        if not hasattr(self.model, "sample_1d"):
            raise RuntimeError(
                "OneDTransitionRewardModel requires wrapped model to define method sample_1d"
            )
        preds, next_model_state, log_prob = self.model.sample_1d(
            model_in, model_state, rng=rng, deterministic=deterministic
        )
        next_observs = preds[:, :-1] if self.learned_rewards else preds
        if self.target_is_delta:
            tmp_ = next_observs + obs
            for dim in self.no_delta_list:
                tmp_[:, dim] = next_observs[:, dim]
            next_observs = tmp_
        rewards = preds[:, -1:] if self.learned_rewards else None
        next_model_state["obs"] = next_observs
        return next_observs, rewards, None, next_model_state, log_prob

    def reset(
        self, obs: torch.Tensor, rng: Optional[torch.Generator] = None
    ) -> Dict[str, torch.Tensor]:
        """Calls reset on the underlying model.

        Args:
            obs (tensor): the observation from which the trajectory will be
                started. The actual value is ignore, only the shape is used.
            rng (`torch.Generator`, optional): an optional random number generator
                to use.

        Returns:
            (dict(str, tensor)): the model state necessary to continue the simulation.
        """
        if not hasattr(self.model, "reset_1d"):
            raise RuntimeError(
                "OneDTransitionRewardModel requires wrapped model to define method reset_1d"
            )
        obs = model_util.to_tensor(obs).to(self.device)
        model_state = {"obs": obs}
        model_state.update(self.model.reset_1d(obs, rng=rng))
        return model_state

    def save(self, save_dir: Union[str, pathlib.Path]):
        self.model.save(save_dir)
        if self.input_normalizer:
            self.input_normalizer.save(save_dir)

    def load(self, load_dir: Union[str, pathlib.Path]):
        self.model.load(load_dir)
        if self.input_normalizer:
            self.input_normalizer.load(load_dir)

    def set_elite(self, elite_indices: Sequence[int]):
        self.model.set_elite(elite_indices)

    def __len__(self):
        return len(self.model)

    def set_propagation_method(self, propagation_method: Optional[str] = None):
        if isinstance(self.model, Ensemble):
            self.model.set_propagation_method(propagation_method)