from app.agents.graph import _decide
from app.core.config import settings


def base(**kw):
    state = {"message": "the printer is jammed", "redacted_message": "the printer is jammed",
             "intent": "incident", "priority": "P3", "confidence": 0.9, "resolves": True,
             "risk_flags": [], "is_outage": False, "requires_human": False}
    state.update(kw)
    return state


def test_outage_escalates():
    assert _decide(base(is_outage=True)) == "escalate"


def test_p1_escalates():
    assert _decide(base(priority="P1")) == "escalate"


def test_confident_answer_resolves_from_kb():
    assert _decide(base(confidence=0.95)) == "finalize_kb_answer"


def test_low_confidence_creates_ticket():
    assert _decide(base(confidence=0.1, resolves=False)) == "create_ticket"


def test_mid_confidence_with_gap_clarifies():
    state = base(confidence=0.5, resolves=False, missing="which application")
    assert _decide(state) == "clarify"


def test_destructive_flag_never_auto_resolves():
    assert _decide(base(risk_flags=["destructive_action"])) == "create_ticket"


def test_password_request_hits_automation():
    state = base(message="I need to reset my password", redacted_message="I need to reset my password",
                 intent="service_request")
    assert _decide(state) == "run_automation"


def test_threshold_is_configurable():
    assert 0 < settings.min_confidence_to_auto_resolve <= 1
