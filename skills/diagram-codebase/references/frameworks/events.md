# Events, queues, and async messaging

Celery, Kafka, RabbitMQ/AMQP, MQTT, Redis pub/sub and streams, SQS/SNS, cloud
pub/sub, WebSockets, in-process event buses, and signals.

## Look for

- **Producers**: `send`/`produce`/`publish`/`emit`/`delay`/`apply_async`/
  `basic_publish`/`xadd`; webhooks the system sends.
- **Consumers**: `@app.task`, `@shared_task`, consumer groups and `subscribe`,
  `basic_consume`, `on_message`, `xreadgroup`, framework listeners
  (`@KafkaListener`), WebSocket handlers.
- **Channel names**: topic/queue/routing-key/exchange strings, often in config or
  constants; follow them to their definition.
- **Broker setup**: connection URLs, exchanges and bindings, partitions, dead
  letter queues, retry policies, schedulers (Celery beat, cron).
- **Delivery semantics**: ack/nack, idempotency keys, ordering, retries with
  backoff, DLQ routing: these are the error paths.
- **Worker processes**: separate entry points (`celery worker`, consumer
  `main`), their concurrency settings, and deployment units that run them.

## Map to the model

| Code | Node / edge |
|---|---|
| Broker | `datastore` or `external_service` (`store:redis`, `ext:kafka`), whichever the team operates |
| Topic/queue/routing key | `event_channel` `chan:<name>` |
| Producer call | `publishes` producer → channel (phase `runtime`; payload type in `payload`) |
| Consumer registration | `subscribes` channel → handler (registration evidence, phase `init`) |
| Celery `task.delay()` | `publishes` caller → `chan:<task queue>`; `subscribes` → task function |
| Worker process | `worker` node; `spawns`/`launches` from its deployment unit |
| Retry / DLQ | `error_path` edges; flow steps of kind `error` |
| Scheduled job | `triggers` from the scheduler to the task |

## Pitfalls

- Channel names built at runtime (f-strings, tenant prefixes): record the pattern
  (`chan:orders.{tenant}`) and an uncertainty.
- A broker client import or connection setup is not a producer or consumer.
- Only one side in the repo is normal for integrations; model the side you have
  and name the other in `uncertainties`.
- Producer → consumer is never a `calls` edge; the channel sits between them, and
  the flow step kind is `async`.
- In-process event buses and signals are still async boundaries for diagrams
  even when they execute synchronously; label which.
