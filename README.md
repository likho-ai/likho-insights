# likho-insights

The insights service of [Likho](https://github.com/likho-ai). For every transcribed call it asks
a language model what the call was about and keeps the answer:

| Part | What it is |
| --- | --- |
| Summary | A few sentences: who called about what, what was said, how it ended |
| Intent and products | What the customer wanted, and the product names mentioned |
| Sentiment | The customer's mood by the end: positive, neutral, negative or mixed |
| Checks | The auditor's yes/no observations, each answered with the line that shows it |
| Scores | The auditor's scored points, each with a reason, and their total |

The checks and the scored points are the company's own quality form (`config/qa.local.json`);
the repository ships an example. The interface is `likho.insights.v1.InsightsService` in
[likho-contracts](https://github.com/likho-ai/likho-contracts): `GetInsights` (by recording or
by transcript), `Analyse` (now, or again with `force`) and `GetStatus` (which model answers, and
whether text may leave at all).

## No text leaves without a key

The transcript's text is sent to the model's API. That only happens when `ANTHROPIC_API_KEY`
is set. Without it the service starts, answers `GetStatus` with `enabled: false`, keeps serving
the insights it already has, and analyses nothing: no event is taken, no text leaves the
machine. The key goes in `.env.<env>.local`, which git ignores, after the company has said yes
to sending transcripts to the model's provider.

What is sent: the lines of one transcript in their Hinglish layer (a time code and the words),
the language detected, and the form's checks and scored points. Not sent: the audio, the
recording's attributes, names of agents or customers beyond what the words themselves say.
Transcripts longer than `MAX_TRANSCRIPT_CHARS` are cut with a note, and the insights record
that they were (`transcript_cut`).

## Run it

Needs the [likho-infra](https://github.com/likho-ai/likho-infra) stack (MongoDB and NATS) and
likho-transcription, where the transcripts are fetched from.

```bash
uv sync
uv run likho-insights            # gRPC on 5050, health on 4050
```

With Docker, on the stack's network:

```bash
docker build -t likho-insights .
docker run --rm --network likho -p 5050:5050 -p 4050:4050 \
  -e MONGO_URL=mongodb://mongo:27017 -e NATS_URL=nats://nats:4222 \
  -e TRANSCRIPTION_GRPC_ADDR=likho-transcription:5020 \
  -e ANTHROPIC_API_KEY=... likho-insights
```

## Configuration

Settings come from environment variables and from `.env` files chosen by `LIKHO_ENV`
(`development` by default). The files are read in this order, each overriding the one before,
and a real environment variable wins over all of them:

```
.env   .env.local   .env.<LIKHO_ENV>   .env.<LIKHO_ENV>.local
```

`.env.development`, `.env.staging` and `.env.production` are committed and hold no secrets.
`.env.<env>.local` holds the secrets of that environment on your machine; git ignores it, and
`likho-infra/scripts/make-env-secrets.py` makes it. In Kubernetes the same values come from
ConfigMaps and Secrets.

| Variable | Default | Meaning |
| --- | --- | --- |
| `GRPC_PORT` | `5050` | gRPC, including the standard health service |
| `HTTP_PORT` | `4050` | `GET /healthz` (alive), `GET /readyz` (MongoDB answers), `GET /metrics` |
| `MONGO_URL` | `mongodb://localhost:27017` | MongoDB |
| `MONGO_DATABASE` | `likho_insights` | The database; collection `insights`, one document per transcript |
| `NATS_URL` | `nats://localhost:4222` | Event bus |
| `NATS_CONNECT_TIMEOUT_SECONDS` | `120` | How long the start keeps trying to reach NATS before going on without it |
| `TRANSCRIPTION_GRPC_ADDR` | `localhost:5020` | likho-transcription, where transcripts are fetched from |
| `RPC_TIMEOUT_SECONDS` | `10` | How long a fetch may take |
| `ANTHROPIC_API_KEY` | empty | The model's key. Empty: nothing is analysed and no text leaves |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` | Which Claude answers |
| `MODEL_TIMEOUT_SECONDS` | `120` | How long one answer may take (two retries inside the client) |
| `MODEL_MAX_OUTPUT_TOKENS` | `2000` | The longest answer accepted |
| `MAX_TRANSCRIPT_CHARS` | `24000` | The longest transcript sent; longer ones are cut with a note |
| `REPORT_LANGUAGE` | `English` | The language the summary and the reasons are written in |
| `QA_FORM_FILE` | `config/qa.example.json` | The auditor's form; the company's own is `config/qa.local.json` |
| `CONSUMERS_ENABLED` | `true` | Analyse every transcript the workers complete, and forget deleted recordings |
| `CONSUMER_GROUP` | `likho-insights` | Durable consumers `<group>-completed` and `<group>-deleted`; instances with the same name share the work |
| `CONSUMER_START` | `new` | `all` takes the stream from its start (analyses every transcript ever completed: it costs) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | empty | Also push the metrics there (OTLP/HTTP) |
| `LOG_LEVEL` | `INFO` | Logs are JSON, one object per line |

## The form

`QA_FORM_FILE` is a JSON file with a version, the checks and the scored points:

```json
{
  "version": "example-1",
  "checks": [{ "key": "greeting", "label": "The agent greeted the customer and named the company" }],
  "scores": [{ "key": "communication", "label": "Clarity and tone of the agent", "max": 5 }]
}
```

Keys are `snake_case` and unique; at most 40 of each. The model answers every check with
`yes`, `no` or `na` (the call gave no way to tell) and the line it rests on, and every scored
point with a number from 0 to its `max` and a reason. The answer is read strictly against the
form: a check the model skipped is `na`, a score it skipped is 0 with a note, a score above
the maximum is clamped. The insights carry `form_version`, so a change of the form is visible
on every result made before and after it.

## Behaviour worth knowing

* **One result per transcript.** `Analyse` without `force` returns the stored insights when
  they exist; with `force` the model is asked again and the newer result replaces the older
  one. `GetInsights` by recording gives the latest.
* **Events.** `likho.insights.completed.v1` goes out after every analysis (the ids, the model,
  the sentiment, the score and the token counts, not the text); `likho.insights.failed.v1`
  when a transcript taken from the bus could not be analysed and will not be retried
  (`transcript_not_found`, `empty_transcript`, `bad_answer`). A model that is down or rate
  limited is retried by the bus (five deliveries, ten seconds apart). A deleted recording
  (`likho.recording.deleted`) loses its insights.
* **Errors over gRPC.** `NOT_FOUND` (no transcript, or no insights yet), `FAILED_PRECONDITION`
  (no model configured; an empty transcript), `UNAVAILABLE` (the model or likho-transcription
  did not answer: worth a retry), `INTERNAL` (the model did not answer with usable JSON).
* **Metrics** at `GET /metrics`: calls by method and outcome and their duration, analyses by
  outcome, the model's duration, tokens by direction, events handled, and
  `likho_insights_model_enabled` (1 when text may leave).

## Work on it

```bash
uv sync --all-groups
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest                     # the service tests need MongoDB and NATS from likho-infra
```

The tests run the real service on free ports with a fake model and a stand-in
likho-transcription, so no key and no network are needed. Without the stack the service tests
are skipped (CI sets `LIKHO_REQUIRE_STACK=1`, so there they fail instead).
