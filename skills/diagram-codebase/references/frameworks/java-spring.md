# Java / Spring

The scanner has no Java parser: Java files are inventoried (so you can cite them)
but have no scanned `cls:`/`fn:` nodes. Create the ones your flows need, using the
id conventions (`cls:src/main/java/com/acme/OrderService.java:OrderService`,
`fn:<path>:OrderService.place`), each with evidence. Say in `uncertainties` that
Java structure comes from your reading only.

## Look for

- Entry: the `@SpringBootApplication` class with `main`; `CommandLineRunner` /
  `ApplicationRunner` beans; `@Scheduled` methods.
- Build: `pom.xml` / `build.gradle(.kts)` modules (subsystem candidates), Spring
  profiles in `application*.yml|properties`.
- Web: `@RestController`/`@Controller` + `@RequestMapping` (class-level prefix) +
  `@GetMapping`/`@PostMapping`...; filters, interceptors, `@ControllerAdvice`
  (error path).
- Wiring: `@Service`/`@Component`/`@Repository`, constructor injection, `@Bean`
  methods in `@Configuration` classes, `@Qualifier`/`@Primary` choosing among
  implementations.
- Persistence: JPA `@Entity` + `@Table`, Spring Data repository interfaces
  (derived query names, `@Query`), Flyway/Liquibase migrations.
- Messaging: `@KafkaListener`, `@RabbitListener`, `@JmsListener`,
  `KafkaTemplate.send`, `ApplicationEventPublisher` + `@EventListener`.
- Async: `@Async`, `CompletableFuture`, WebFlux `Mono`/`Flux`.

## Map to the model

| Code | Node / edge |
|---|---|
| Controller method | `api:<METHOD> <prefix+path>` `handles` → `fn:` |
| Injected bean call | `calls` (the injected type resolves to one implementation → `static_inferred` unless only one exists and the call site is explicit) |
| `@Entity` | `db_entity` `ent:<table>`; repository calls → `db_read`/`db_write` on `store:<db>` |
| `@KafkaListener(topics="x")` / `send("x")` | `chan:x` + `subscribes` / `publishes` |
| `@Scheduled` | `triggers` from a timer concept to the method, phase `runtime` |
| `application.yml` key read via `@Value` / `@ConfigurationProperties` | `cfg:file:application.yml` + `config_dependency` |

## Pitfalls

- Interface-typed injection hides the implementation; check for multiple beans,
  profiles, and `@ConditionalOn...` before naming one.
- AOP proxies (`@Transactional`, `@Cacheable`, security) add behavior invisible at
  the call site; mention it in the flow step label when it matters.
- Spring Data derived queries have no body: cite the interface method as the
  `db_read` evidence.
- Bean definitions are `init`; request handling is `runtime`.
