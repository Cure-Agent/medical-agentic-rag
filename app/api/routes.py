import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.graph import PRESETS, build_graph
from app.agent.nodes import make_default_nodes
from app.agent.state import AgentAnswer, initial_state
from app.llm.client import make_embeddings, make_llm
from app.retrieval.base import Evidence
from app.retrieval.factory import make_retriever

router = APIRouter()


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    preset: str = "full"


class AskResponse(BaseModel):
    result: AgentAnswer
    preset: str
    policy_version: str
    retrieval_count: int
    searched_queries: list[str]
    trace: list[dict]


def _get_agent(request: Request, preset: str):
    if preset not in PRESETS:
        raise HTTPException(status_code=422, detail=f"unknown preset: {preset}")
    agents = request.app.state.agents
    if preset not in agents:
        settings = request.app.state.settings
        cfg = PRESETS[preset]
        llm = make_llm(settings)
        retriever = make_retriever(cfg, request.app.state.pool, make_embeddings(settings), settings)
        agents[preset] = build_graph(cfg, make_default_nodes(cfg, llm, retriever, settings))
    return agents[preset]


def _jsonable(value):
    if isinstance(value, Evidence | AgentAnswer):
        return value.model_dump()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.post("/ask", response_model=AskResponse)
async def ask(body: AskRequest, request: Request) -> AskResponse:
    agent = _get_agent(request, body.preset)
    final = await agent.ainvoke(initial_state(body.question))
    result = final["result"] or AgentAnswer(kind="abstain", text="파이프라인이 결과를 내지 못함")
    cfg = PRESETS[body.preset]
    return AskResponse(
        result=result,
        preset=body.preset,
        policy_version=request.app.state.settings.policy_version_for(
            top_k=cfg.top_k,
            rerank=cfg.enable_rerank,
            distance_cutoff=cfg.distance_cutoff,
            rerank_cutoff=cfg.rerank_score_cutoff if cfg.enable_rerank else None,
            fuse_before_rerank=cfg.fuse_before_rerank,
        ),
        retrieval_count=final["retrieval_count"],
        searched_queries=final["searched_queries"],
        trace=_jsonable(final["trace"]),
    )


@router.post("/ask/stream")
async def ask_stream(body: AskRequest, request: Request):
    """노드 단위 진행 상황을 SSE로 흘린다 — 루프가 도는 모습이 그대로 보이는 데모용."""
    agent = _get_agent(request, body.preset)

    async def event_stream():
        async for update in agent.astream(initial_state(body.question), stream_mode="updates"):
            for node_name, delta in update.items():
                payload = {
                    "node": node_name,
                    "retrieval_count": delta.get("retrieval_count"),
                    "result": _jsonable(delta.get("result")),
                    "trace_tail": _jsonable(delta.get("trace", [])[-1:]),
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
