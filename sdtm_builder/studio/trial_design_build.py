"""Trial-design domains come from protocol extraction (an AI step) in the studio, so there is no
trial-design table offline: Auto-Build's trial-design pass finds nothing to build."""


def sync_trial_design(study_id: str, user_email: str = "system", persist: bool = True) -> dict:
    return {}
