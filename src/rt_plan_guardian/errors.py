"""Exception types. Kept in one module so every layer can import them without cycles."""


class GuardianError(Exception):
    """Base class for errors raised deliberately by RT-Plan Guardian."""


class LoadError(GuardianError):
    """The supplied files cannot be turned into a reviewable case (user-fixable)."""


class ConfigError(GuardianError):
    """The ruleset file is invalid (unknown rule id, bad severity, bad parameter...)."""


class NotApplicable(GuardianError):
    """Raised by a rule: this check does not apply to this plan. Does not affect the verdict."""


class CannotEvaluate(GuardianError):
    """Raised by a rule: this check applies but the data needed is missing or unsupported.

    Reported as SKIPPED - loudly, never as a pass.
    """
