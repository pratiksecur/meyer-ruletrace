"""HTTP endpoints for the local-LLM helpers. Added to the RuleTrace router (same login, roles, audit chain).

    GET  /ruletrace/llm/status                 is Ollama up, is the model pulled
    POST /ruletrace/designs/{id}/explain       plain-language explanation of each failing finding
    POST /ruletrace/rules/draft                regulation text -> DRAFT rule YAML (never saved automatically)
    POST /ruletrace/ask                        grounded Q&A over the rule pack (+ a design's findings)
    POST /ruletrace/designs/{id}/fixes         verified fix suggestions (llm_actions.py)
    POST /ruletrace/designs/{id}/whatif        plain-language what-if -> validated overrides -> diff (llm_actions.py)

The model never decides compliance; see llm_assist.py. Each use is written to the audit chain.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import Depends, HTTPException
import yaml
from pydantic import BaseModel, Field
from vartija_core import audit
from vartija_core.api import Principal

from . import llm_assist
from .engine import RuleError, check_design, load_rules
from .model import DesignError, design_from_dict


def get_client():
    """One place to build the model client (tests replace this with a fake)."""
    return llm_assist.OllamaClient()


class ExplainIn(BaseModel):
    limit: int = Field(default=8, ge=1, le=30, description="Explain at most this many failing findings")
    rule_id: str | None = Field(default=None, description="Only explain the finding(s) of this rule")
    element_id: str | None = Field(default=None, description="...and of this element (used with rule_id)")


class DraftIn(BaseModel):
    text: str = Field(min_length=20, max_length=20000, description="One regulation paragraph or section")
    regime: str = ""
    ref: str = ""
    source_url: str = ""


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    design_id: str | None = Field(default=None, description="Also use this design's current findings")


def add_llm_routes(app, router, engineer, path_of) -> None:
    def _audit(who: Principal, action: str, payload: dict) -> None:
        with app.state.session_factory() as s:
            audit.append(s, action=action, actor=who.name, actor_kind=who.kind, payload=payload)
            s.commit()

    def _result_for(design_id: str) -> tuple[dict, dict]:
        rec = json.loads(path_of(design_id).read_text(encoding="utf-8"))
        try:
            design = design_from_dict(rec["design"], source_file=rec["filename"])
            return rec, check_design(design, load_rules())
        except (DesignError, RuleError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/llm/status")
    def llm_status(who: Principal = Depends(engineer)) -> dict:
        client = get_client()
        return client.status() if hasattr(client, "status") else {"model": client.name, "reachable": True}

    @router.post("/designs/{design_id}/explain")
    def explain(design_id: str, body: ExplainIn | None = None, who: Principal = Depends(engineer)) -> dict:
        rec, result = _result_for(design_id)
        client = get_client()
        findings = result["findings"]
        if body and body.rule_id:  # one finding, e.g. the one the engineer clicked in the viewer
            findings = [f for f in findings if f["rule_id"] == body.rule_id
                        and (body.element_id is None or f.get("element_id") == body.element_id)]
        items = llm_assist.explain_findings(client, findings, (body.limit if body else 8))
        _audit(who, "design.explained", {"design_id": design_id, "design_sha256": rec["sha256"],
                                         "model": client.name, "explained": len(items),
                                         "fell_back_to_rules": sum(1 for i in items if i["source"] == "rules")})
        return {"design_id": design_id, "model": client.name, "counts": result["counts"], "explanations": items}

    @router.post("/rules/draft")
    def draft(body: DraftIn, who: Principal = Depends(engineer)) -> dict:
        client = get_client()
        pack = load_rules()
        existing = {r.id for r in pack}
        vocab = llm_assist.rule_vocabulary([r.__dict__ for r in pack])
        try:
            out = llm_assist.draft_rule(client, body.text, regime=body.regime, ref=body.ref,
                                        source_url=body.source_url, existing_ids=existing, vocabulary=vocab)
        except llm_assist.LLMError as exc:
            raise HTTPException(502, f"language model failed: {exc}") from exc
        for d in out["drafts"]:          # the engine's own loader must accept it too
            if not d["ok"]:
                continue
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "draft.yaml"
                p.write_text(yaml.safe_dump([d["rule"]], sort_keys=False, allow_unicode=True), encoding="utf-8")
                try:
                    load_rules([p])
                except RuleError as exc:
                    d["ok"] = False
                    d["problems"].append(f"rule engine rejected it: {exc}")
        ok_rules = [d["rule"] for d in out["drafts"] if d["ok"]]
        out["yaml"] = yaml.safe_dump(ok_rules, sort_keys=False, allow_unicode=True, width=100) if ok_rules else ""
        _audit(who, "rule.drafted", {"model": client.name, "text_sha256": out["text_sha256"], "regime": body.regime,
                                     "ref": body.ref, "drafts": len(out["drafts"]), "usable": len(ok_rules)})
        return out

    from .llm_actions import add_action_routes  # verified fix suggestions and plain-language what-if
    add_action_routes(app, router, engineer, path_of, lambda: get_client(), _audit)

    @router.post("/ask")
    def ask(body: AskIn, who: Principal = Depends(engineer)) -> dict:
        rules = [r.__dict__ for r in load_rules()]
        findings = _result_for(body.design_id)[1]["findings"] if body.design_id else []
        client = get_client()
        try:
            out = llm_assist.answer_question(client, body.question, rules, findings)
        except llm_assist.LLMError as exc:
            raise HTTPException(502, f"language model failed: {exc}") from exc
        _audit(who, "assistant.asked", {"model": client.name, "design_id": body.design_id,
                                        "question_chars": len(body.question), "cited": [c["rule_id"] for c in out["citations"]]})
        return out
