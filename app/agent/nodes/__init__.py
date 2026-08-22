from langchain_core.language_models import BaseChatModel

from app.agent.graph import AgentConfig, AgentNodes
from app.agent.nodes.answerer import make_abstain_node, make_answer_node
from app.agent.nodes.decomposer import make_decompose_node, make_passthrough_decompose
from app.agent.nodes.evaluator import make_evaluate_node
from app.agent.nodes.query_generator import make_generate_queries_node
from app.agent.nodes.retrieve import make_retrieve_node
from app.config import Settings
from app.retrieval.base import Retriever


def make_default_nodes(
    cfg: AgentConfig, llm: BaseChatModel, retriever: Retriever, settings: Settings
) -> AgentNodes:
    """실제 구현체 조립. decomposition이 꺼진 구성은 LLM을 부르지 않는 passthrough를 쓴다 —
    ablation에서 '껐다'가 '호출하되 무시한다'가 아니라 정말 안 부르는 것이어야 정직하다."""
    return AgentNodes(
        decompose=(
            make_decompose_node(llm) if cfg.enable_decomposition else make_passthrough_decompose()
        ),
        retrieve=make_retrieve_node(retriever),
        answer=make_answer_node(llm, settings),
        abstain=make_abstain_node(),
        evaluate=make_evaluate_node(llm, settings) if cfg.enable_evaluator else None,
        generate_queries=make_generate_queries_node(llm) if cfg.enable_evaluator else None,
    )
