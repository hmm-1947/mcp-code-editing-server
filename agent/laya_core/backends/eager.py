"""The stock PyTorch forward: no padding, no graphs, the reference every other backend is measured against."""
from .base import Backend


class EagerBackend(Backend):
    name = "eager"

    def install(self) -> None:
        # Usually there is nothing to swap: `model.forward` is the class forward. It is not always --
        # the base class documents that a backend may find an instance `forward` already on the model,
        # left by another backend or by the legacy `fast=True` path, which `laya.backends.install`
        # does not own. Pushing that replacement aside is what makes installing eager mean the stock
        # forward answers; leaving it would report `eager` from `agent.backend` while a graph backend
        # still ran every request.
        if self.installed:
            return
        self._prior_forward = self.model.__dict__.pop("forward", None)
        self.installed = True

    def uninstall(self) -> None:
        # The replacement goes back exactly as the base contract has it, so eager stacked over
        # another backend leaves that backend's work in place rather than a half-switched model.
        if not self.installed:
            return
        if self._prior_forward is not None:
            self.model.forward = self._prior_forward
        self._prior_forward = None
        self.installed = False
