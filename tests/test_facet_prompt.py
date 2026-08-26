"""축 열거 변형(3차 실험)의 통제 조건을 잠근다.

이 실험의 주장은 "**프롬프트 한 군데만** 바꿨는데 회수가 달라졌다"이다. 그러려면
(a) 기본 프롬프트가 이전과 한 글자도 다르지 않아야 하고 (b) 새 프리셋이 검색 정책을
건드리지 않아야 한다. 통제군 설정에서 조용히 어긋나는 실패는 실제로 겪었다 —
근거 개수 통제군이 리랭커 요청 개수 고정 때문에 당시 실험 통제군과 같아진 적이 있다.
"""

from dataclasses import asdict

from langchain_core.language_models import BaseChatModel

from app.agent.graph import PRESETS
from app.agent.nodes import make_default_nodes
from app.config import Settings
from app.llm.prompts import ANSWERER_SYSTEM, ANSWERER_SYSTEM_FACETS, answerer_system
from tests.conftest import FakeRetriever

SEARCH_FIELDS = (
    "enable_decomposition",
    "enable_evaluator",
    "enable_rerank",
    "fuse_before_rerank",
    "top_k",
    "distance_cutoff",
    "rerank_score_cutoff",
    "max_retrieval",
)


def test_default_answerer_prompt_is_untouched():
    """기본값은 기존 상수 그대로여야 한다 — 아니면 3차 실험이 기존 결과와 비교 불가다."""
    assert answerer_system(enumerate_facets=False) == ANSWERER_SYSTEM
    assert ANSWERER_SYSTEM not in ANSWERER_SYSTEM_FACETS  # 중간에 삽입되므로 부분문자열이 아니다


def test_facet_variant_only_adds_the_axis_step():
    """변형은 축 열거 단계를 더한 것뿐이다. 나머지 규칙이 바뀌면 원인이 갈린다."""
    added = len(ANSWERER_SYSTEM_FACETS) - len(ANSWERER_SYSTEM)
    assert added > 0
    assert "축을 먼저 빠짐없이 나열한다" in ANSWERER_SYSTEM_FACETS
    # 기권 규칙은 양쪽에 그대로 있어야 한다 — 이 단계가 과잉 기권을 늘리면 실험이 오염된다
    for prompt in (ANSWERER_SYSTEM, ANSWERER_SYSTEM_FACETS):
        assert "부분 답변은 기권이 아니다" in prompt
    assert "축이 하나라도 답되면 기권이 아니다" in ANSWERER_SYSTEM_FACETS


def test_rerank_facets_differs_from_rerank_only_in_the_flag():
    base, variant = PRESETS["rerank"], PRESETS["rerank_facets"]
    changed = {k for k, v in asdict(base).items() if v != asdict(variant)[k]}
    assert changed == {"enumerate_facets"}
    for field in SEARCH_FIELDS:
        assert getattr(base, field) == getattr(variant, field), field


class _RecordingLLM(BaseChatModel):
    """with_structured_output만 쓰이므로 그 반환값이 프롬프트를 기록하게 한다."""

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(self, *args, **kwargs):  # pragma: no cover - 호출되지 않는다
        raise NotImplementedError

    def with_structured_output(self, schema, **kwargs):
        return self


def _system_prompt_for(preset: str) -> str:
    """노드를 조립해 answerer가 실제로 들고 있는 시스템 프롬프트를 꺼낸다."""
    cfg = PRESETS[preset]
    llm = _RecordingLLM()
    nodes = make_default_nodes(cfg, llm, FakeRetriever({}), Settings())
    # answer 노드는 클로저다 — 캡처된 system 변수를 들여다본다
    return nodes.answer.__closure__[
        nodes.answer.__code__.co_freevars.index("system")
    ].cell_contents


def test_flag_reaches_the_answer_node():
    """플래그가 config에만 있고 노드에 안 닿으면, 두 구성이 같은 프롬프트로 돌면서
    '차이 없음'이라는 잘못된 결론을 낸다."""
    assert _system_prompt_for("rerank") == ANSWERER_SYSTEM
    assert _system_prompt_for("rerank_facets") == ANSWERER_SYSTEM_FACETS
