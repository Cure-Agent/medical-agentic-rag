"""③ Evidence Evaluator — 근거 풀이 질문에 답하기 충분한지 판정한다."""

from langchain_core.language_models import BaseChatModel

from app.agent.state import AgentState, EvaluatorVerdict, QueryCoverage, trace_event
from app.config import Settings
from app.llm.prompts import EVALUATOR_SYSTEM, format_evidence_block
from app.retrieval.base import Evidence


def _format_pool(evidence: dict[str, Evidence], cap: int) -> str:
    # 라운드가 쌓여도 프롬프트가 무한히 자라지 않게 거리 오름차순 상위 cap개만 싣는다
    best = sorted(evidence.values(), key=lambda e: e.distance)[:cap]
    return "\n\n".join(
        format_evidence_block(
            e.chunk_id, e.guideline_title, e.section_title, e.content,
            e.recommendation_grade, e.evidence_level,
        )
        for e in best
    )


def make_evaluate_node(llm: BaseChatModel, settings: Settings):
    structured = llm.with_structured_output(EvaluatorVerdict)

    async def evaluate(state: AgentState) -> dict:
        # 근거 0건은 LLM에게 물을 것도 없다 — 결정론적으로 불충분 판정 (비용·안전 모두 이득)
        if not state["evidence"]:
            verdict = EvaluatorVerdict(
                sufficient=False,
                per_query=[
                    QueryCoverage(query=q, covered=False) for q in state["searched_queries"]
                ],
                missing_aspects=[state["question"]],
            )
            return {
                "verdict": verdict,
                "trace": state["trace"]
                + [trace_event("evaluate", sufficient=False, short_circuit="empty_pool")],
            }

        sub_queries = "\n".join(f"- {q}" for q in state["searched_queries"])
        pool = _format_pool(state["evidence"], settings.max_evidence_for_llm)
        user = (
            f"## 질문\n{state['question']}\n\n"
            f"## 검색에 사용한 하위 질의\n{sub_queries}\n\n"
            f"## 검색된 근거 청크\n{pool}"
        )
        verdict: EvaluatorVerdict = await structured.ainvoke(
            [("system", EVALUATOR_SYSTEM), ("user", user)]
        )
        return {
            "verdict": verdict,
            "trace": state["trace"]
            + [
                trace_event(
                    "evaluate",
                    sufficient=verdict.sufficient,
                    missing_aspects=verdict.missing_aspects,
                )
            ],
        }

    return evaluate
