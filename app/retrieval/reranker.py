"""LLM 리스트와이즈 리랭커 — cure-agent-be `openai-reranker.ts`(docs/specs/29) 포팅.

후보 전체(발췌 300자)를 한 번에 주고 상위 5개 순위 + top-1 관련도(0~10)를 JSON으로 받는다.
운영 실측: Recall@5 0.780 → 0.983, 지연 p50 +0.96s.

이식하며 지킨 것:
- 발췌 300자 상한 — 전문을 실으면 입력이 부풀고 실측이 이 값으로 났다.
- 관대한 순위 파싱 — 모델이 번호를 문자열("3")로 내는 것이 운영에서 관측됐다.
  잘못 걸러도 최악이 코사인 폴백이라 관대한 쪽이 안전하다.
- **빈 순위는 오류가 아니다** — 관련 후보 없음 판정({"ranking":[null],"top1Relevance":0})이며
  점수 게이트가 기권으로 처리한다. 여기서 던지면 무관 질문이 기권 대신 폴백으로 «답변»된다.
"""

import json

from openai import AsyncOpenAI
from pydantic import BaseModel

EXCERPT_LENGTH = 300
RERANK_TOP_N = 5  # 운영 기본값 — §29 실측(Recall@5 0.983)이 이 개수에서 났다


def build_system_prompt(top_n: int) -> str:
    """요청 개수만 파라미터다.

    운영은 항상 5개지만, 근거 개수 통제군(`rerank_topk11`)은 리랭커가 11개를 돌려줘야
    한다. 5로 고정하면 top_k를 올려도 결과가 5개에 머물러 통제군이 당시 실험 기준군과
    같아진다 — 통제 변인이 사라지는 결함이다.
    """
    return "\n".join(
        [
            "당신은 한의 임상 지침 검색 결과를 재정렬하는 리랭커입니다.",
            "질문과 후보 근거 목록이 주어집니다. 질문에 실제로 답하는 근거인지로 판정하세요 —",
            "어휘가 겹쳐도 다른 질환·다른 주제면 관련이 없습니다.",
            f'JSON만 출력: {{"ranking":[가장 관련 있는 후보 번호 {top_n}개, 관련도 순],'
            '"top1Relevance":0~10 정수}',
            "top1Relevance는 1위 후보가 질문에 답하는 정도입니다 (0=무관, 10=직접 답변).",
        ]
    )


SYSTEM_PROMPT = build_system_prompt(RERANK_TOP_N)


class RerankCandidate(BaseModel):
    chunk_id: str
    content: str
    guideline_title: str


class RerankResult(BaseModel):
    order: list[str]  # 관련도 순 chunk_id
    top1_relevance: float


class RerankerError(RuntimeError):
    """호출측은 이 예외를 잡아 코사인 순위로 폴백한다 — 재시도보다 싸고 빠르다."""


def normalize_ranking(ranking: object, candidate_count: int) -> list[int]:
    """순위 배열 정규화 — 정수 변환, 범위 밖·중복 제거. 1-기반 번호를 돌려준다."""
    if not isinstance(ranking, list):
        return []
    seen: set[int] = set()
    indices: list[int] = []
    for entry in ranking:
        # bool은 int의 하위형이라 먼저 걸러야 True가 1로 통과하지 않는다
        if isinstance(entry, bool) or entry is None:
            continue
        try:
            number = float(entry)
        except (TypeError, ValueError):
            continue
        # TS의 Number.isInteger와 같은 의미 — "3"은 통과, 3.5는 탈락
        if not number.is_integer():
            continue
        value = int(number)
        if value < 1 or value > candidate_count or value in seen:
            continue
        seen.add(value)
        indices.append(value)
    return indices


class OpenAiReranker:
    def __init__(self, api_key: str, model: str, top_n: int = RERANK_TOP_N):
        # 리랭크 1회 입력이 후보 57개 기준 ~11k 토큰이라 TPM 버스트가 쉽게 터진다
        # (분해 구성 실측: 429로 36문항 중 10건 실패). SDK 재시도는 Retry-After를 따른다.
        self._client = AsyncOpenAI(api_key=api_key or None, max_retries=6)
        self._model = model
        self._system_prompt = build_system_prompt(top_n)

    @property
    def model(self) -> str:
        return self._model

    async def rerank(self, question: str, candidates: list[RerankCandidate]) -> RerankResult:
        listing = "\n".join(
            f"[{i + 1}] ({c.guideline_title}) {c.content[:EXCERPT_LENGTH]}"
            for i, c in enumerate(candidates)
        )
        response = await self._client.chat.completions.create(
            model=self._model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": f"질문: {question}\n\n후보:\n{listing}"},
            ],
        )
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise RerankerError("리랭커 응답에 본문이 없습니다")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as error:
            raise RerankerError(f"리랭커 응답이 JSON이 아닙니다: {content[:120]}") from error

        relevance = parsed.get("top1Relevance") if isinstance(parsed, dict) else None
        try:
            relevance_value = float(relevance)
        except (TypeError, ValueError) as error:
            raise RerankerError(
                f"리랭커 응답에 top1Relevance가 없습니다: {content[:120]}"
            ) from error

        indices = normalize_ranking(parsed.get("ranking"), len(candidates))
        return RerankResult(
            order=[candidates[i - 1].chunk_id for i in indices], top1_relevance=relevance_value
        )
