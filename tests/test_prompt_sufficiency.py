"""Non-circular checks. The repo's simulator reads ORDER_SNAPSHOT and nothing else, so passing it
cannot show that a prompt is *sufficient*. Here a strict stand-in model decides ONLY from what is
visible in the prompt text (POLICIES + ORDER_SNAPSHOT) and fails if something it needs is missing.
Ablations then prove the prompt carries exactly what the decision needs: no less, no more."""
from __future__ import annotations

import itertools
import json
import re

import pytest

import question1.agent as q1mod
import question2.agent as q2mod
from eval.reference import decide_order
from question1.agent import SupportCopilot
from question2.agent import SupportPipeline
from shared.billing import CostSession
from shared.prompting import POLICY_FIELDS

HOUSE = ["returns-within-30-days", "damage-replacement-7-days", "custom-items-no-replace",
         "digital-license-no-refund", "subscription-cooling-off"]


def _visible(prompt: str):
    """What a model can actually see: article ids in POLICIES and the facts JSON."""
    policies = set(re.findall(r"^\[([a-z0-9-]+)\] ", prompt, flags=re.M))
    facts = json.loads(re.search(r"ORDER_SNAPSHOT:(\{.*?\})END_SNAPSHOT", prompt, re.S).group(1))
    return policies, facts


def prompt_only_decision(prompt: str):
    """Apply the written policies using ONLY visible text. Returns (action, article) or None."""
    policies, f = _visible(prompt)
    ptype = f.get("product_type")
    need = None
    if ptype == "subscription":
        need = ("REFUND" if int(f.get("subscription_age_days", 0)) <= 30 else "SEND_ARTICLE",
                "subscription-cooling-off")
    elif f.get("license_revealed"):
        need = ("SEND_ARTICLE", "digital-license-no-refund")
    elif f.get("customized") and f.get("damage_reported"):
        need = ("ESCALATE_TIER2", "custom-items-no-replace")
    elif f.get("damage_reported") and int(f.get("delivered_days_ago", 99)) <= 7:
        need = ("REPLACE", "damage-replacement-7-days")
    elif ptype == "physical" and not f.get("opened") and int(f.get("delivered_days_ago", 99)) <= 30:
        need = ("REFUND", "returns-within-30-days")
    else:
        need = ("ESCALATE_TIER2", "returns-within-30-days")
    # a model cannot cite or apply a policy it was never shown
    return need if need[1] in policies else None


class PromptOnlyLlm:
    """Drop-in for LlmClient: answers from the prompt text alone, never from files or the order."""

    def __init__(self, session, model="prompt-only"):
        self.session = session

    def chat(self, label, messages, **_):
        prompt = "\n\n".join(m.content for m in messages)
        decision = prompt_only_decision(prompt)
        out = json.dumps({"action": decision[0], "kb_article": decision[1]}) if decision else "I cannot tell."
        self.session.record(label, prompt, out)
        return out


def order(**kw):
    base = dict(order_id="o", sku="s", title="t", product_type="physical", delivered_days_ago=10,
                opened=False, license_revealed=False, customized=False, damage_reported=False,
                subscription_age_days=10)
    base.update(kw)
    return base


def all_orders():
    for pt, lic, cust, dmg, opn, days, sub in itertools.product(
        ["physical", "digital", "subscription", "custom"], [0, 1], [0, 1], [0, 1], [0, 1],
        [1, 7, 8, 30, 31], [10, 30, 31]):
        yield order(product_type=pt, license_revealed=bool(lic), customized=bool(cust),
                    damage_reported=bool(dmg), opened=bool(opn), delivered_days_ago=days,
                    subscription_age_days=sub)


@pytest.fixture
def prompt_only(monkeypatch):
    for mod in (q1mod, q2mod):
        monkeypatch.setattr(mod, "LlmClient", PromptOnlyLlm)


def run(cls, o, monkeypatch):
    tk = {"ticket_id": "T", "order_id": "o", "messages": [{"from": "customer", "text": "refund"}]}
    for mod in (q1mod, q2mod):
        monkeypatch.setattr(mod, "get_ticket", lambda _t, tk=tk: tk)
        monkeypatch.setattr(mod, "get_order", lambda _i, o=o: o)
    return cls(session=CostSession()).handle_ticket("T")


@pytest.mark.parametrize("cls", [SupportCopilot, SupportPipeline])
def test_prompt_alone_is_sufficient_for_every_order(cls, prompt_only, monkeypatch):
    """960 orders: a model that sees only the prompt reaches the reference decision."""
    for o in all_orders():
        got = run(cls, o, monkeypatch)
        exp = decide_order(o)
        assert (got.action, got.kb_article) == (exp.action, exp.kb_article), o


def test_the_stand_in_model_really_is_prompt_only():
    """Guard against a vacuous test: withhold the needed article and the stand-in cannot answer."""
    prompt = "POLICIES:\n[subscription-cooling-off] x\nORDER_SNAPSHOT:{\"product_type\":\"physical\",\"opened\":false,\"delivered_days_ago\":3}END_SNAPSHOT"
    assert prompt_only_decision(prompt) is None  # returns article missing -> cannot cite it


@pytest.mark.parametrize("field", POLICY_FIELDS)
def test_every_sent_order_field_is_necessary(field):
    """Dropping any one field changes the correct decision for at least one order."""
    changed = False
    for o in all_orders():
        trimmed = {k: v for k, v in o.items() if k != field}
        a, b = decide_order(o), decide_order(trimmed)
        if (a.action, a.kb_article) != (b.action, b.kb_article):
            changed = True
            break
    assert changed, f"{field} is sent but never matters"


@pytest.mark.parametrize("field", ["order_id", "sku", "title"])
def test_identifier_fields_never_matter_so_they_are_not_sent(field):
    for o in itertools.islice(all_orders(), 0, None, 7):
        trimmed = {k: v for k, v in o.items() if k != field}
        assert decide_order(o) == decide_order(trimmed)
    assert field not in POLICY_FIELDS


def _drop_needed_article(monkeypatch):
    monkeypatch.setattr(q1mod, "_policy_article_ids", lambda _o: ["returns-within-30-days"])
    monkeypatch.setattr(SupportPipeline, "_retrieve", lambda self, t: ["returns-within-30-days"])


def test_ablation_q1_without_the_needed_policy_the_model_cannot_answer(prompt_only, monkeypatch):
    _drop_needed_article(monkeypatch)
    with pytest.raises(ValueError):  # model says "cannot tell" -> unparseable, never a silent guess
        run(SupportCopilot, order(customized=True, damage_reported=True, delivered_days_ago=2), monkeypatch)


def test_ablation_q2_without_the_needed_policy_fails_closed(prompt_only, monkeypatch):
    _drop_needed_article(monkeypatch)
    got = run(SupportPipeline, order(customized=True, damage_reported=True, delivered_days_ago=2), monkeypatch)
    assert got.action == "ESCALATE_TIER2" and got.validation_error  # visible, recoverable failure
