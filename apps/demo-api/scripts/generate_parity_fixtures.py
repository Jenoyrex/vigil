"""Generate apps/demo-api parity fixtures from the PRODUCTION Python code.

The demo API re-implements a few production behaviors in TypeScript. These
fixtures are produced by running the real production modules (read-only) so
the demo's tests assert against production output, not against a guess:

  relevance  -- services/evaluator's TF-IDF RelevanceEvaluator + sklearn's
                English stop-word list. Run with services/worker's venv
                (it installs the evaluator and scikit-learn):
                  services/worker/.venv/Scripts/python.exe \
                    apps/demo-api/scripts/generate_parity_fixtures.py relevance
  api        -- apps/api's ingestion transform, pydantic request validation,
                deterministic sampling, and Python float/JSON formatting.
                Run with apps/api's venv from the repository root:
                  VIGIL_API_INTERNAL_SERVICE_TOKEN=x apps/api/.venv/Scripts/python.exe \
                    apps/demo-api/scripts/generate_parity_fixtures.py api

Output: apps/demo-api/test/fixtures/*.json. Re-run after any change to the
production evaluator, ingestion, or request schemas.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parents[1] / "test" / "fixtures"

RELEVANCE_CASES: list[tuple[str, str, float | None]] = [
    ("What does Vigil do for an LLM application?",
     "Vigil records traces from an LLM application and evaluates whether each response is "
     "relevant to its input.", None),
    ("What is the capital of France?", "The capital of France is Paris.", None),
    ("What is the capital of France?", "Bananas are an excellent source of potassium.", None),
    ("How do I reset my password?", "Go to settings and click reset password.", 0.2),
    ("How do I reset my password?", "Go to settings and click reset password.", 0.9),
    ("the and of", "a an the", None),
    ("!!! ???", "... ,,,", None),
    ("12 34 5", "12 34", None),
    ("   ", "some output", None),
    ("some input", "\n\t ", None),
    ("", "", None),
    ("Python python PYTHON", "python", None),
    ("snake_case_identifier and another_one", "snake_case_identifier", None),
    ("don't stop-believing, it's well-known", "dont stop believing well known", None),
    ("Café naïve résumé déjà vu", "cafe naive resume deja vu café", None),
    ("東京 は 日本 の 首都 です", "東京 首都", None),
    ("emoji 🚀 rocket launch 🚀", "rocket 🚀 launch", None),
    ('{"question":"refund policy","lang":"en"}', '{"answer":"refunds within 30 days"}', None),
    ("repeat repeat repeat unique", "repeat unique unique unique", None),
    ("x y z a b c", "q w e r t", None),
    ("ΣΟΦΙΑ σοφία ΟΔΟΣ", "σοφια οδος", None),
    ("İstanbul Straße", "istanbul strasse", None),
    ("tab\tseparated\nnewline text", "separated text newline", None),
    ("I", "a", None),
    ("machine learning model evaluation metrics precision recall",
     "precision and recall are evaluation metrics for machine learning models", None),
    ("threshold edge", "threshold edge", 1.0),
    ("threshold edge", "threshold", 0.0),
    ("\x1c\x1dhidden separators\x1e", "hidden separators", None),
    (chr(0xA0) + chr(0x2003), "text", None),
]
RELEVANCE_LONG = ("alpha beta gamma delta " * 400, "gamma delta epsilon zeta " * 300)


def relevance() -> None:
    sys.path.insert(0, str(ROOT / "services" / "evaluator"))
    from app.relevance import RelevanceEvaluator, RelevanceEvaluatorInput
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

    evaluator = RelevanceEvaluator()
    cases = []
    for input_text, output_text, threshold in [*RELEVANCE_CASES, (*RELEVANCE_LONG, None)]:
        result = evaluator.evaluate(
            RelevanceEvaluatorInput(input_text=input_text, output_text=output_text),
            threshold=threshold,
        )
        cases.append(
            {
                "input": input_text,
                "output": output_text,
                "threshold": threshold,
                "score": result.score,
                "label": result.label,
                "explanation": result.explanation,
                "evaluator_name": result.evaluator_name,
                "evaluator_version": result.evaluator_version,
                "evaluator_model": result.evaluator_model,
            }
        )
    _write("relevance-parity.json", {"cases": cases})
    _write("english-stop-words.json", sorted(ENGLISH_STOP_WORDS))


INGEST_PAYLOADS: list[str] = [
    # valid: minimal
    '{"spans":[{"trace_id":"0123456789ABCDEF0123456789abcdef","span_id":"0123456789ABCDEF",'
    '"name":"  root  ","start_time":"2026-09-28T05:00:00Z","end_time":"2026-09-28T05:00:01.2345Z"}]}',
    # valid: full LLM span with resource extras, JSON input, attributes, events
    '{"resource":{"service.name":"svc","sdk.name":"vigil-python","sdk.version":"0.1.0",'
    '"region":"eu","replicas":3,"ratio":1.0,"flag":true,"meta":{"a":[1,2.5,"x"]}},'
    '"spans":[{"trace_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","span_id":"bbbbbbbbbbbbbbbb",'
    '"parent_span_id":"cccccccccccccccc","name":"llm call","span_type":"llm",'
    '"start_time":"2026-09-28T05:00:00+05:30","end_time":"2026-09-28T05:00:02+05:30",'
    '"status":"ok","status_message":null,"input":{"messages":[{"role":"user","content":"héllo \\"q\\""}],'
    '"temperature":0.7,"n":1,"big":12345678901234567890,"tiny":1e-7,"whole":2.0},'
    '"output":"hi","attributes":{"k":"v","i":7,"f":0.5,"b":false,"e":1e16},'
    '"events":[{"time":"2026-09-28T05:00:01Z","name":"first_token","attributes":{"ms":120}}],'
    '"llm_provider":"openai","llm_model":"gpt-x","llm_input_tokens":10,"llm_output_tokens":5,'
    '"llm_total_tokens":15,"llm_cost_usd":0.0012345678,"environment":"prod","release":"v1"}]}',
    # valid: naive datetimes, unix timestamps, service_name by field name
    '{"resource":{"service_name":"by-field-name"},"spans":[{"trace_id":"'
    + "1" * 32
    + '","span_id":"'
    + "2" * 16
    + '","name":"n","start_time":"2026-09-28T05:00:00","end_time":1790571601.5}]}',
    # valid: both timestamps naive (treated as UTC by storage)
    '{"spans":[{"trace_id":"' + "5" * 32 + '","span_id":"' + "6" * 16 + '","name":"n",'
    '"start_time":"2026-09-28T05:00:00","end_time":"2026-09-28T05:00:00.5"}]}',
    # valid: cost as integer, tokens as zero-fraction float
    '{"spans":[{"trace_id":"' + "3" * 32 + '","span_id":"' + "4" * 16 + '","name":"n",'
    '"start_time":"2026-09-28T05:00:00Z","end_time":"2026-09-28T05:00:00Z",'
    '"llm_provider":"p","llm_cost_usd":2,"llm_input_tokens":3.0}]}',
    # invalid cases
    '{"spans":[]}',
    '{}',
    '{"spans":[{"trace_id":"xyz","span_id":"0123456789abcdef","name":"n",'
    '"start_time":"2026-09-28T05:00:00Z","end_time":"2026-09-28T05:00:00Z"}]}',
    '{"spans":[{"trace_id":"' + "a" * 32 + '","span_id":"0123","name":"n",'
    '"start_time":"2026-09-28T05:00:00Z","end_time":"2026-09-28T05:00:00Z"}]}',
    '{"spans":[{"trace_id":"' + "a" * 32 + '","span_id":"' + "b" * 16 + '","name":"   ",'
    '"start_time":"2026-09-28T05:00:00Z","end_time":"2026-09-28T05:00:00Z"}]}',
    '{"spans":[{"trace_id":"' + "a" * 32 + '","span_id":"' + "b" * 16 + '","name":"n",'
    '"start_time":"2026-09-28T05:00:01Z","end_time":"2026-09-28T05:00:00Z"}]}',
    '{"spans":[{"trace_id":"' + "a" * 32 + '","span_id":"' + "b" * 16 + '",'
    '"start_time":"2026-09-28T05:00:00Z"}]}',
    '{"spans":[{"trace_id":"' + "a" * 32 + '","span_id":"' + "b" * 16 + '","name":"n",'
    '"start_time":"not-a-date","end_time":"2026-09-28T05:00:00Z","status":"bad",'
    '"llm_input_tokens":-1,"llm_cost_usd":-0.5,"attributes":{"x":null}}]}',
    '{"spans":[{"trace_id":"' + "a" * 32 + '","span_id":"' + "b" * 16 + '","name":5,'
    '"start_time":"2026-09-28T05:00:00Z","end_time":"2026-09-28T05:00:00Z","span_type":""}]}',
    '{"resource":{"service.name":7},"spans":[{"trace_id":"' + "a" * 32 + '","span_id":"'
    + "b" * 16 + '","name":"n","start_time":"2026-09-28T05:00:00Z",'
    '"end_time":"2026-09-28T05:00:00Z","llm_output_tokens":1.5}]}',
]


def _truncation_payload() -> str:
    big_ascii = "a" * (64 * 1024 + 10)
    # 3-byte UTF-8 character straddling the 64 KiB boundary exercises the
    # errors="ignore" partial-character drop.
    big_multibyte = "x" + "€" * 30000
    attributes = {f"attr_{i:04d}": "v" * 200 for i in range(700)}
    events = [{"time": "2026-09-28T05:00:00Z", "name": f"e{i}", "attributes": {"k": "v" * 500}}
              for i in range(300)]
    return json.dumps({
        "spans": [{
            "trace_id": "d" * 32, "span_id": "e" * 16, "name": "big",
            "start_time": "2026-09-28T05:00:00Z", "end_time": "2026-09-28T05:00:00Z",
            "input": big_ascii, "output": big_multibyte, "attributes": attributes,
            "events": events,
        }]
    }, ensure_ascii=False)


def _ms(value: datetime) -> int:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return (aware - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


def _row_to_json(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if key == "project_id":
            continue
        if isinstance(value, datetime):
            out[key] = _ms(value)
        elif key == "events.time":
            out[key] = [_ms(v) for v in value]
        elif key == "llm_cost_usd":
            # Decimal64(6): clickhouse_connect writes int(Decimal(str(x)) * 10**6).
            out["llm_cost_micros"] = None if value is None else int(value * 10**6)
        else:
            out[key] = value
    return out


def api() -> None:
    import uuid

    sys.path.insert(0, str(ROOT / "apps" / "api"))
    from pydantic import TypeAdapter, ValidationError

    from app.schemas.auth import LoginRequest, SignupRequest
    from app.schemas.traces import TracesRequest
    from app.schemas.workspace import NameRequest
    from app.services.evaluations import _is_sampled_in
    from app.services.ingestion import transform_request

    project_id = uuid.UUID("11111111-2222-3333-4444-555555555555")

    def errors(exc: ValidationError) -> list[dict]:
        return [{"type": e["type"], "loc": ["body", *e["loc"]], "msg": e["msg"]}
                for e in exc.errors(include_url=False)]

    ingest = []
    for text in [*INGEST_PAYLOADS, _truncation_payload()]:
        try:
            payload = TracesRequest.model_validate(json.loads(text))
        except ValidationError as exc:
            ingest.append({"body": text, "errors": errors(exc)})
            continue
        except TypeError as exc:
            # Production bug (not fixed here): comparing a naive and an aware
            # datetime in SpanIn._validate_ids_and_times raises TypeError -> HTTP 500.
            ingest.append({"body": text, "production_server_error": str(exc)})
            continue
        rows = transform_request(payload, project_id=project_id)
        ingest.append({"body": text, "rows": [_row_to_json(r) for r in rows]})
    _write("ingest-parity.json", {"cases": ingest})

    requests = []
    for model, body in [
        (SignupRequest, {"email": "  User@Example.COM ", "password": "correct horse battery"}),
        (SignupRequest, {"email": "not-an-email", "password": "short"}),
        (SignupRequest, {"email": "a@b.co", "password": "x" * 201, "full_name": "y" * 201}),
        (SignupRequest, {"password": "correct horse battery"}),
        (SignupRequest, {"email": "a b@c.de", "password": "correct horse battery"}),
        (LoginRequest, {"email": "a@b.co", "password": ""}),
        (NameRequest, {"name": "   "}),
        (NameRequest, {"name": "  Acme  "}),
        (NameRequest, {"name": "z" * 201}),
        (NameRequest, {}),
    ]:
        try:
            parsed = model.model_validate(body)
            requests.append({"model": model.__name__, "body": body,
                             "parsed": parsed.model_dump(mode="json")})
        except ValidationError as exc:
            requests.append({"model": model.__name__, "body": body, "errors": errors(exc)})
    _write("request-validation-parity.json", {"cases": requests})

    sampling = []
    for i in range(200):
        trace_id = f"{i:032x}"
        span_id = f"{i * 7919:016x}"
        for rate in (0.0, 0.1, 0.5, 0.9999, 1.0):
            sampling.append({
                "project_id": str(project_id), "trace_id": trace_id, "span_id": span_id,
                "evaluator_name": "relevance", "sampling_rate": rate,
                "sampled": _is_sampled_in(project_id=project_id, trace_id=trace_id,
                                          span_id=span_id, evaluator_name="relevance",
                                          sampling_rate=rate),
            })
    _write("sampling-parity.json", {"cases": sampling})

    float_values = [0.0, -0.0, 1.0, -2.5, 0.1, 1e16, 1e15, 1e-5, 0.0001, 1.5e-7, 123456789012345.6,
                    1.7976931348623157e308, 5e-324, 2.0 ** 53, 0.30000000000000004, 1e22, 100.0,
                    3.14159, 1e21, 12345.678e10]
    float_repr = [{"value": v, "repr": repr(v)} for v in float_values]
    json_texts = ['{"a":1.0,"b":[1,2.50,-0,1E3,1e-7],"c":"é\\n\\u0001\\"","d":null,"e":true}',
                  '[12345678901234567890, 0.1, -0.0, "\\u2028"]',
                  '"plain string"', '{"nested":{"k":[{},[]]}}']
    json_dumps = [{"text": t, "dumps": json.dumps(json.loads(t), ensure_ascii=False,
                                                  separators=(",", ":"))} for t in json_texts]
    datetimes = []
    for text in ["2026-09-28T05:00:00Z", "2026-09-28T05:00:00.123456+05:30", "2026-09-28 05:00:00",
                 "2026-09-28", "2026-09-28T05:00", "1790571601", "1790571601500", "bad",
                 "2026-02-30T00:00:00Z", "2026-09-28T05:00:00.1234567Z", "2026-09-28T24:00:00Z"]:
        try:
            value = TypeAdapter(datetime).validate_python(text)
            datetimes.append({"text": text, "ms": _ms(value), "aware": value.tzinfo is not None})
        except ValidationError as exc:
            datetimes.append({"text": text, "error": exc.errors(include_url=False)[0]["msg"]})
    _write("python-format-parity.json",
           {"float_repr": float_repr, "json_dumps": json_dumps, "datetimes": datetimes})


def _write(name: str, data: object) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    {"relevance": relevance, "api": api}[sys.argv[1]]()
