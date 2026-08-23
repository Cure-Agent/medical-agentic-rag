"""Answer / Abstain — 종료 노드. abstain은 LLM 없이 결정론적으로 만든다."""

import re

from langchain_core.language_models import BaseChatModel

from app.agent.state import AgentAnswer, AgentState, AnswererOutput, Citation, trace_event
from app.config import Settings
from app.llm.prompts import ABSTAIN_TEMPLATE, answerer_system, format_evidence_block
from app.retrieval.base import Evidence


def _citations_in(text: str, evidence: dict[str, Evidence]) -> list[Citation]:
    # 스키마 강제 대신 본문 인용 표기를 역파싱한다 — 인용은 답변 텍스트와 붙어 있어야
    # 임상의에게 의미가 있고, 풀에 없는 id는 인용으로 인정하지 않는다.
    # 실주행에서 LLM이 [id1, id2]처럼 한 괄호에 여러 id를 묶는 것이 실측되어,
    # 괄호 내용물을 쉼표·공백으로 쪼개 개별 id로 매칭한다.
    cited: set[str] = set()
    for group in re.findall(r"\[([^\[\]]+)\]", text):
        cited.update(token for token in re.split(r"[,\s]+", group) if token in evidence)
    return [
        Citation(
            chunk_id=chunk_id,
            guideline_title=item.guideline_title,
            section_title=item.section_title,
        )
        for chunk_id, item in evidence.items()
        if chunk_id in cited
    ]


def make_answer_node(llm: BaseChatModel, settings: Settings, *, enumerate_facets: bool = False):
    """생성 단계 안전 게이트를 겸한다.

    검색 게이트는 관련도만 재므로 인접 주제(군발두통↔편두통)를 통과시킨다 — 실측에서
    insufficient 12문항 중 3건이 컷 9를 넘었고, 그때 실제로 막아선 것은 answerer가
    산문으로 거부한 것이었다. 그 판단을 구조화해 받아 `kind`로 승격한다.

    기권 본문은 LLM 텍스트가 아니라 ABSTAIN_TEMPLATE로 만든다 — 결정론 유지에 더해,
    거부하면서 다른 질환 정보를 곁들이던 실측 사례(ins-008)가 출력되지 않는다.
    """
    structured = llm.with_structured_output(AnswererOutput)
    system = answerer_system(enumerate_facets=enumerate_facets)

    async def answer(state: AgentState) -> dict:
        best = sorted(state["evidence"].values(), key=lambda e: e.distance)
        best = best[: settings.max_evidence_for_llm]
        pool = "\n\n".join(
            format_evidence_block(
                e.chunk_id, e.guideline_title, e.section_title, e.content,
                e.recommendation_grade, e.evidence_level,
            )
            for e in best
        )
        user = f"## 질문\n{state['question']}\n\n## 근거 청크\n{pool}"
        output: AnswererOutput = await structured.ainvoke([("system", system), ("user", user)])

        # 빈 답변은 플래그와 무관하게 기권이다 — 모델이 플래그를 빠뜨려도 빈 본문은 못 낸다.
        # 발화 원인을 구분해 남긴다: 과잉 기권을 조사할 때 「모델이 판단해서 기권」과
        # 「응답이 비어서 기권」은 원인도 대책도 다르다 (실측 cpx-007은 후자로 보였다).
        empty_answer = not output.answer.strip()
        if output.insufficient_evidence or empty_answer:
            missing = output.missing_aspects
            missing_text = "\n".join(f"- {m}" for m in missing) or "- (근거 부족)"
            result = AgentAnswer(
                kind="abstain",
                text=ABSTAIN_TEMPLATE.format(missing=missing_text),
                missing_aspects=missing,
            )
            return {
                "result": result,
                "trace": state["trace"]
                + [
                    trace_event(
                        "answer",
                        gated="empty_answer" if empty_answer else "insufficient_evidence",
                        flag=output.insufficient_evidence,
                        missing_aspects=missing,
                    )
                ],
            }

        result = AgentAnswer(
            kind="answer",
            text=output.answer,
            citations=_citations_in(output.answer, state["evidence"]),
        )
        return {
            "result": result,
            "trace": state["trace"]
            + [trace_event("answer", citations=[c.chunk_id for c in result.citations])],
        }

    return answer


def make_abstain_node():
    async def abstain(state: AgentState) -> dict:
        verdict = state["verdict"]
        missing = verdict.missing_aspects if verdict and verdict.missing_aspects else []
        missing_text = "\n".join(f"- {m}" for m in missing) or "- (근거 검색 결과가 충분하지 않음)"
        result = AgentAnswer(
            kind="abstain",
            text=ABSTAIN_TEMPLATE.format(missing=missing_text),
            missing_aspects=missing,
        )
        return {
            "result": result,
            "trace": state["trace"] + [trace_event("abstain", missing_aspects=missing)],
        }

    return abstain
