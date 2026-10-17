"""Language-model clients.

``GroqClient`` is the only class that talks to an external service. It is built
only when the configuration allows it (see ``Settings.engine_problems``), always
uses the configured model - ``openai/gpt-oss-20b`` - and refuses a response that
reports a different model rather than accepting a substitute.

Nothing here logs prompts, responses or document text, and error messages never
include them: an ``LLMError`` carries a stable code, a generic message and, for
rate limits, how long the service asked us to wait.

``ScriptedClient`` and ``ReplayClient`` stand in for Groq in tests and in the
evaluation harness, so no test needs network access or an API key.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from app.core.config import REQUIRED_GROQ_MODEL, Settings, settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class LLMResponse:
    content: str | None
    finish_reason: str | None
    model: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: int = 0


class LLMError(Exception):
    """A failed model call. ``fatal`` errors stop the whole run (auth, wrong model ...)."""

    def __init__(self, code: str, message: str, *, retry_after: float | None = None, fatal: bool = False,
                 schema_path: str | None = None, schema_rule: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.fatal = fatal
        #: For a rejected format: where the answer broke the schema (a JSON pointer such as
        #: /accounts/0/fields/3/name) and which rule (enum, required, type ...). Structure only, never content.
        self.schema_path = schema_path
        self.schema_rule = schema_rule


class LLMClient(Protocol):
    model: str

    def complete(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str) -> LLMResponse: ...


def _base_model(name: str | None) -> str:
    return (name or "").rsplit("/", 1)[-1].strip().lower()


def same_model(requested: str, reported: str | None) -> bool:
    """Groq reports the model that served the call; it must be the one requested."""
    if not reported:
        return True  # nothing reported: nothing contradicts the request
    return reported == requested or _base_model(reported) == _base_model(requested)


class GroqClient:
    """Groq chat completions with strict JSON-schema output."""

    def __init__(self, cfg: Settings = settings) -> None:
        problems = cfg.engine_problems("groq")
        if problems:
            first = problems[0]
            raise LLMError(first["code"], first["message"], fatal=True)
        from groq import Groq  # imported only when external calls are allowed

        self.cfg = cfg
        self.model = cfg.groq_model
        assert self.model == REQUIRED_GROQ_MODEL  # guaranteed by engine_problems
        self._client = Groq(
            api_key=cfg.groq_api_key.get_secret_value(),  # type: ignore[union-attr]
            timeout=cfg.groq_timeout_seconds,
            max_retries=cfg.groq_max_retries,
        )

    def complete(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str) -> LLMResponse:
        import groq

        started = time.perf_counter()
        try:
            completion = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
                temperature=0,
                seed=self.cfg.groq_seed,
                reasoning_effort=self.cfg.groq_reasoning_effort,
                include_reasoning=False,
                max_completion_tokens=self.cfg.groq_max_completion_tokens,
            )
        except groq.RateLimitError as exc:
            raise LLMError("llm_rate_limited", "Groq rate limit reached.", retry_after=_retry_after(exc)) from None
        except groq.APITimeoutError:
            raise LLMError("llm_timeout", "Groq did not answer in time.") from None
        except groq.APIConnectionError:
            raise LLMError("llm_connection", "Could not connect to Groq.") from None
        except groq.AuthenticationError:
            raise LLMError("llm_auth", "Groq rejected the API key.", fatal=True) from None
        except groq.PermissionDeniedError:
            raise LLMError("llm_permission", "The Groq API key may not use this model.", fatal=True) from None
        except groq.NotFoundError:
            raise LLMError("llm_model_unavailable", f"Groq does not offer {self.model} to this key.", fatal=True) from None
        except groq.BadRequestError as exc:
            code = _error_code(exc)
            if code == "json_validate_failed":
                path, rule = _schema_violation(exc)
                raise LLMError("llm_schema_rejected", "The model's answer did not match the required format.",
                               schema_path=path, schema_rule=rule) from None
            raise LLMError("llm_bad_request", f"Groq rejected the request ({code or 'bad request'}).") from None
        except groq.APIStatusError as exc:
            raise LLMError("llm_server_error", f"Groq returned an error (HTTP {exc.status_code}).") from None
        except groq.GroqError:
            raise LLMError("llm_error", "The Groq call failed.") from None

        latency = int((time.perf_counter() - started) * 1000)
        if not same_model(self.model, getattr(completion, "model", None)):
            raise LLMError("llm_model_mismatch", "Groq answered with a different model; the answer was discarded.", fatal=True)
        choice = completion.choices[0] if completion.choices else None
        usage = completion.usage
        details = getattr(usage, "completion_tokens_details", None) if usage else None
        return LLMResponse(
            content=choice.message.content if choice else None,
            finish_reason=choice.finish_reason if choice else None,
            model=getattr(completion, "model", None),
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            reasoning_tokens=getattr(details, "reasoning_tokens", 0) or 0,
            latency_ms=latency,
        )


def _retry_after(exc: Any) -> float | None:
    try:
        headers = exc.response.headers
        if headers.get("retry-after-ms"):
            return float(headers["retry-after-ms"]) / 1000
        if headers.get("retry-after"):
            return float(headers["retry-after"])
    except Exception:
        return None
    return None


_VIOLATION = re.compile(r"'(/[A-Za-z0-9_/]*)' does not validate with (/[A-Za-z0-9_/]*)")


def _schema_violation(exc: Any) -> tuple[str | None, str | None]:
    """The JSON pointer and schema keyword from Groq's validator message ("'/accounts/0/fields/3/name' does not
    validate with /properties/.../enum: ..."). Only the pointer and the keyword are kept, never the answer."""
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else None
    text = error.get("message") if isinstance(error, dict) else None
    match = _VIOLATION.search(text) if isinstance(text, str) else None
    if not match:
        return None, None
    return match.group(1), match.group(2).rstrip("/").rsplit("/", 1)[-1] or None


def _error_code(exc: Any) -> str | None:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error["code"]
    return None


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


Step = LLMResponse | LLMError | Callable[[str, str], LLMResponse]


class ScriptedClient:
    """Returns pre-written responses in order. For tests: no network, no key."""

    def __init__(self, steps: list[Step], model: str = REQUIRED_GROQ_MODEL) -> None:
        self.steps = list(steps)
        self.model = model
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str) -> LLMResponse:
        self.calls.append({"system": system, "user": user, "schema_name": schema_name})
        if not self.steps:
            raise LLMError("llm_error", "Scripted client has no response left.")
        step = self.steps.pop(0)
        if isinstance(step, LLMError):
            raise step
        if callable(step) and not isinstance(step, LLMResponse):
            return step(system, user)
        return step


def response_json(payload: dict[str, Any], *, finish_reason: str = "stop", input_tokens: int = 1000,
                  output_tokens: int = 400, model: str = REQUIRED_GROQ_MODEL) -> LLMResponse:
    """A canned model answer (for tests and simulated fixtures)."""
    return LLMResponse(json.dumps(payload), finish_reason, model, input_tokens, output_tokens, 0, 5)


def _key(system: str, user: str, schema_name: str) -> str:
    return hashlib.sha256(f"{schema_name}\n{system}\n{user}".encode("utf-8")).hexdigest()[:32]


class ReplayClient:
    """Answers from responses recorded earlier by ``RecordingClient``.

    A prompt that was never recorded fails with ``llm_replay_missing`` instead
    of silently producing something.
    """

    def __init__(self, directory: Path, model: str = REQUIRED_GROQ_MODEL) -> None:
        self.directory = Path(directory)
        self.model = model

    def complete(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str) -> LLMResponse:
        path = self.directory / f"{_key(system, user, schema_name)}.json"
        if not path.exists():
            raise LLMError("llm_replay_missing", "No recorded response for this prompt.")
        data = json.loads(path.read_text(encoding="utf-8"))
        return LLMResponse(**data["response"])


class RecordingClient:
    """Wraps a real client and stores each response for later replay.

    Only for synthetic or explicitly authorised documents: recordings contain
    the model's answer, i.e. values copied from the document.
    """

    def __init__(self, inner: LLMClient, directory: Path) -> None:
        self.inner = inner
        self.model = inner.model
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def complete(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str) -> LLMResponse:
        response = self.inner.complete(system=system, user=user, schema=schema, schema_name=schema_name)
        path = self.directory / f"{_key(system, user, schema_name)}.json"
        path.write_text(json.dumps({"recorded": True, "response": asdict(response)}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        return response
