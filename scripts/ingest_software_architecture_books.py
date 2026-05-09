#!/usr/bin/env python3
"""
ingest_software_architecture_books.py — Ingest 7 essential software architecture books
into Nova's memory database.

Books:
  1. Clean Architecture — Robert C. Martin
  2. Designing Data-Intensive Applications — Martin Kleppmann
  3. Fundamentals of Software Architecture — Mark Richards & Neal Ford
  4. Software Architecture: The Hard Parts — Ford, Richards, Sadalage, Dehghani
  5. Head First Software Architecture — Satin, Richards, Ford
  6. System Design Interview: An Insider's Guide — Alex Xu
  7. Software Engineering at Google — Winters, Manshreck, Wright

Posts progress to #nova-notifications every 5 minutes.
Source: "software_architecture_books"

Written by Jordan Koch.
"""

import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

MEMORY_SERVER  = "http://127.0.0.1:18790"
SOURCE         = "software_architecture"
BATCH_DELAY    = 0.3   # seconds between individual memory pushes
NOTIFY_EVERY   = 300   # post to Slack every 5 minutes

LOG_FILE = Path.home() / ".openclaw/logs/ingest_software_arch.log"

_last_notify = time.time()
_total_memories = 0
_current_book = ""
_start_time = time.time()


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def post_notify(msg: str):
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)


def maybe_notify(force: bool = False):
    global _last_notify
    now = time.time()
    if force or (now - _last_notify) >= NOTIFY_EVERY:
        elapsed = int(now - _start_time)
        m, s = divmod(elapsed, 60)
        post_notify(
            f":books: *Software Architecture Books — Ingest Update*\n"
            f"• Current book: *{_current_book}*\n"
            f"• Memories stored so far: *{_total_memories}*\n"
            f"• Elapsed: {m}m {s}s"
        )
        _last_notify = now


def remember(text: str, metadata: dict = None):
    global _total_memories
    payload = json.dumps({
        "text": text.strip(),
        "source": SOURCE,
        "metadata": metadata or {},
    }).encode()
    req = urllib.request.Request(
        f"{MEMORY_SERVER}/remember",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=30)
        _total_memories += 1
    except Exception as e:
        log(f"  ERROR storing memory: {e}")
    time.sleep(BATCH_DELAY)
    maybe_notify()


def ingest_book(title: str, author: str, year: int, memories: list[str]):
    global _current_book
    _current_book = f"{title} ({author})"
    log(f"\n{'='*60}")
    log(f"Ingesting: {title}")
    log(f"Author: {author} · Year: {year}")
    log(f"Memories to store: {len(memories)}")
    meta = {"book": title, "author": author, "year": year}
    for i, mem in enumerate(memories, 1):
        remember(mem, meta)
        if i % 10 == 0:
            log(f"  {i}/{len(memories)} stored...")
    log(f"  Done: {len(memories)} memories stored")


# ── BOOKS ─────────────────────────────────────────────────────────────────────

BOOKS = [

    # ── 1. Clean Architecture ─────────────────────────────────────────────────
    {
        "title": "Clean Architecture: A Craftsman's Guide to Software Structure and Design",
        "author": "Robert C. Martin (Uncle Bob)",
        "year": 2017,
        "memories": [
            "Clean Architecture's central principle: the goal of software architecture is to minimize the human resources required to build and maintain the required system. Good architecture makes the system easy to change — not just to build.",
            "Uncle Bob's dependency rule: source code dependencies must point only inward, toward higher-level policies. Nothing in an inner circle can know anything about an outer circle. Database frameworks, UI, and external agencies live in outer circles. Business rules live in the center.",
            "Clean Architecture defines four concentric rings: Entities (enterprise business rules), Use Cases (application business rules), Interface Adapters (controllers, gateways, presenters), and Frameworks & Drivers (web, databases, devices). The database is a detail, not the center.",
            "The Dependency Inversion Principle applied architecturally: high-level policy should never depend on low-level detail. Both should depend on abstractions. This is what makes architecture stable in the face of change.",
            "Uncle Bob argues that the web is a delivery mechanism, not an architecture. If your architecture is defined by the framework you use (Rails, Spring, Django), you have no architecture — you have a framework serving as your architecture.",
            "Screaming Architecture: your architecture should scream what the application does, not what framework it uses. A healthcare system's top-level folder structure should look like healthcare, not like MVC or REST.",
            "The distinction between policy and detail: policy is the business rules, the essence. Detail is everything that makes policy execute — databases, web frameworks, IoT sensors. Architecture separates them.",
            "Boundaries in Clean Architecture: a boundary separates things that matter from things that don't at different levels of the architecture. You draw a boundary where the axis of change differs on each side.",
            "The Humble Object pattern: split code that is hard to test (tightly coupled to environment) from code that is easy to test. The Humble Object contains the hard-to-test part with almost no logic. Everything interesting moves to the testable side.",
            "Uncle Bob on databases: 'The database is a detail. The data model is a detail. The database schema is a detail.' Business entities should be POJOs/POCOs with no database annotations or ORM dependencies.",
            "The four architectural patterns Uncle Bob traces from Clean Architecture: Layered, Hexagonal (Ports and Adapters), DCI (Data, Context, Interaction), and BCE (Boundary, Control, Entity). All share the same objective: separation of concerns.",
            "Component principles — REP (Reuse/Release Equivalence): the granule of reuse is the granule of release. CCP (Common Closure): gather into components those classes that change for the same reasons and at the same times. CRP (Common Reuse): don't force users to depend on things they don't need.",
            "Stable Dependencies Principle: depend in the direction of stability. A component that others depend on must be more stable than the components that depend on it.",
            "Stable Abstractions Principle: stable components should be abstract; unstable components should be concrete. The more stable a component is, the more abstract it should be. The more unstable, the more concrete.",
            "The Main component as the dirtiest of all details: Main is the outermost circle, the framework that creates and coordinates all the others. It is the ultimate detail, the lowest-level policy. It sets up global variables, creates factories, and injects dependencies.",
            "Uncle Bob on tests in architecture: tests are part of the system and they participate in the architecture. Tests that are not designed as part of the system tend to be fragile and tend to make the system rigid and difficult to change.",
            "The case for two values in software: behavior (making the software work as the stakeholders require) and architecture (making the software easy to change). Most developers focus on behavior and ignore architecture — this is wrong. Architecture is more important.",
            "Uncle Bob on frameworks: frameworks are tools, not ways of life. Frameworks solve problems developers have in common. They save time in the short term. But they often impose their structural decisions on your architecture. Be skeptical. Use them but don't couple to them.",
            "The Interactor (Use Case object) in Clean Architecture: an Interactor contains application-specific business rules. It orchestrates the flow of data to and from Entities, and directs those Entities to use their enterprise-wide business rules to achieve the goals of the Use Case.",
            "Presenters and View Models: the output of a Use Case is a simple data structure called the Output Data object. The Presenter takes this and formats it as a View Model, which is a another simple data structure suitable for display. The View then renders the View Model without logic.",
            "On microservices and Clean Architecture: Clean Architecture applies at any scale. Whether you have one monolith or 50 microservices, the dependency rule still applies within each service. Microservices that are poorly architected internally are just a distributed ball of mud.",
            "The Plugin Architecture: Clean Architecture describes a plugin architecture where lower-level components can be plugged in and replaced without affecting higher-level components. Database plugins, UI plugins, web framework plugins — all replaceable.",
            "Uncle Bob on agile and architecture: the agile movement has often led teams to deprioritize architecture in favor of 'working software.' This is a false dichotomy. You can write clean, well-architected code in sprints. Technical debt compounds with interest.",
            "Boundaries and the cost of crossing them: every boundary has a cost. Boundaries between components, between services, between functions all carry overhead. The art of architecture is knowing where to draw boundaries and which direction the dependencies should flow across them.",
        ],
    },

    # ── 2. Designing Data-Intensive Applications ───────────────────────────────
    {
        "title": "Designing Data-Intensive Applications: The Big Ideas Behind Reliable, Scalable, and Maintainable Systems",
        "author": "Martin Kleppmann",
        "year": 2017,
        "memories": [
            "Kleppmann defines three core concerns of data-intensive systems: reliability (the system works correctly even when things go wrong), scalability (as the system grows, there are reasonable ways to deal with growth), and maintainability (over time, different people can work on the system productively).",
            "Hardware faults, software errors, and human errors are the three categories of system faults. Hardware faults have historically been the most common; software systematic errors are often harder to anticipate. Human errors — configuration mistakes, deploying buggy code — are the leading cause of outages in production.",
            "The data model is one of the most important decisions in software. It determines what is easy to do and what is difficult. Most applications are built on top of a general-purpose data model like SQL. But this model isn't optimal for all use cases.",
            "Relational vs. document models: relational databases provide better support for joins, many-to-one, and many-to-many relationships. Document databases are better when data has a document-like structure (a tree of one-to-many relationships), when you want schema flexibility, or when you need better performance due to locality.",
            "Graph data models: if your data has many-to-many relationships, the graph model is the most natural. Property graph model (Neo4j) and triple-store model (SPARQL) are the two most common. Social networks, web graphs, road networks, and dependency graphs are classic graph use cases.",
            "Storage engines: log-structured merge-tree (LSM-tree) storage engines like LevelDB, RocksDB, and Cassandra are optimized for writes. B-tree storage engines (used in most relational databases) are optimized for reads. Each has different performance characteristics for different workloads.",
            "Write-ahead log (WAL): a fundamental technique in storage engines and databases. Before any changes are made to the actual data structures, they are written to a log file. This ensures durability — even if the system crashes mid-operation, the log can be replayed to recover.",
            "OLTP vs. OLAP: Online Transaction Processing (OLTP) databases handle low-latency queries that touch a small number of records (typical web applications). Online Analytical Processing (OLAP) databases handle complex analytical queries over large datasets. Column-oriented storage is optimized for OLAP.",
            "Data encoding and evolution: backward compatibility (new code reads data written by old code) and forward compatibility (old code reads data written by new code) are critical for deploying changes without downtime. JSON, XML, and Avro/Protobuf/Thrift have very different evolvability properties.",
            "Replication: keeping a copy of the same data on multiple machines to improve availability, read throughput, and tolerate failures. Single-leader, multi-leader, and leaderless replication are the three main approaches. Each involves different trade-offs around consistency and conflict resolution.",
            "Replication lag: in asynchronous single-leader replication, followers may lag behind the leader by seconds or minutes. Read-your-own-writes consistency, monotonic reads consistency, and consistent prefix reads are specific guarantees you can provide to users to mitigate the problems lag causes.",
            "Partitioning (sharding): splitting a large dataset into partitions that can be stored on different nodes. Range-based partitioning enables efficient range queries but risks hot spots. Hash-based partitioning distributes load evenly but makes range queries inefficient.",
            "The problem of skewed workloads: if one key gets far more read/writes than others (celebrity user, viral post), that partition becomes a hot spot. Application-level sharding — appending random numbers to hot keys and combining results — is one mitigation strategy.",
            "Transactions and ACID: Atomicity (the transaction either fully commits or fully rolls back), Consistency (the database is always in a valid state), Isolation (concurrently executing transactions are isolated from each other), Durability (committed data is persisted). ACID is a set of guarantees, not a specific implementation.",
            "Weak isolation levels: read committed (prevents dirty reads and dirty writes), snapshot isolation (each transaction reads from a consistent snapshot), and read uncommitted (no isolation). Most databases do not use full serializability by default due to performance costs.",
            "Serializable isolation — the gold standard: guarantees that transactions execute as if they were run serially. Three approaches: actual serial execution (single-threaded), two-phase locking (2PL), and serializable snapshot isolation (SSI, an optimistic concurrency control approach).",
            "The CAP theorem: a distributed system can provide at most two of Consistency, Availability, and Partition tolerance. Since network partitions cannot be avoided in distributed systems, the real choice is between consistency and availability during a partition.",
            "Linearizability: the strongest consistency model. The system behaves as if there is only one copy of the data, even if it's replicated. All operations take effect atomically at some point in time. Linearizability is expensive and incompatible with high availability during network partitions.",
            "Causal consistency: if operation A happened before operation B, any system that has seen B must also have seen A. Weaker than linearizability but can be maintained across all replicas even during partitions. Many practical systems aim for causal consistency rather than linearizability.",
            "Consensus algorithms: fundamental to distributed systems. Paxos, Multi-Paxos, Raft, and Zab all solve the problem of getting multiple nodes to agree on a value despite failures. Consensus is used for leader election, atomic commits, and coordination services (ZooKeeper, etcd).",
            "Batch processing with MapReduce: divide a large job into map (extract key-value pairs), shuffle (group by key), and reduce (aggregate per key) phases. The Unix philosophy of simple tools chained together scales to massive datasets when applied to distributed computing.",
            "Stream processing: unbounded data sets arriving continuously over time. Kafka, Flink, and Samza process events as they arrive. Event sourcing — storing the log of all events rather than the current state — is a powerful pattern that aligns stream processing with the source of truth.",
            "Lambda architecture: combining batch and stream processing. A batch layer computes accurate results on historical data; a speed layer computes approximate results on recent data in real time; a serving layer merges results. Criticized for complexity — Kappa architecture simplifies by using stream processing for both.",
            "The stream-table duality: a table is a stream of upserts, accumulated over time. A stream is a table that changes over time. This duality underlies much of the power in systems like Kafka Streams and Flink — databases and message queues are two sides of the same coin.",
            "Derived data systems: most large systems have a combination of systems of record (the source of truth, typically a normalized relational database) and derived data systems (caches, search indexes, data warehouses, recommendation systems). Keeping derived data consistent with the source of truth is a central challenge.",
            "The future of data systems: Kleppmann argues for treating all data systems as specialized in different ways rather than seeking a single general-purpose database. The dataflow paradigm — modeling systems as transformations on streams of events — provides a unifying abstraction.",
        ],
    },

    # ── 3. Fundamentals of Software Architecture ──────────────────────────────
    {
        "title": "Fundamentals of Software Architecture: An Engineering Approach",
        "author": "Mark Richards and Neal Ford",
        "year": 2020,
        "memories": [
            "Richards and Ford define software architecture as the structure of a system, the characteristics those structures must support, the decisions that have been made, and the rationale behind those decisions. Everything in software is a trade-off.",
            "The four dimensions of software architecture: structure (monolith, microservices, etc.), architectural characteristics (scalability, performance, security, etc.), architectural decisions (the rules governing how the system is built), and design principles (guidelines rather than hard rules).",
            "Architectural characteristics (also called non-functional requirements, quality attributes, or '-ilities'): availability, reliability, testability, scalability, security, agility, fault tolerance, elasticity, recoverability, performance, deployability, learnability. Architects must prioritize — you can't optimize for all of them simultaneously.",
            "Explicitly vs. implicitly defined architectural characteristics: explicit characteristics are stated in requirements ('the system must process 10,000 transactions per second'). Implicit characteristics are assumed but not stated ('users expect to log in securely'). Architects must identify both.",
            "Modularity is the fundamental building block of architectural thinking. Cohesion (how related the things inside a module are) and coupling (how dependent modules are on each other) are the two primary dimensions. High cohesion + low coupling is the canonical goal.",
            "Metrics for modularity: abstractness (ratio of abstract elements to concrete), instability (ratio of outgoing coupling to total coupling), and distance from the main sequence (a module should be either abstract or unstable, not both abstract and stable or both concrete and unstable).",
            "Connascence: a more detailed vocabulary than coupling for describing how tightly components are connected. Static connascence (e.g., same name, same type) is preferable to dynamic connascence (e.g., same execution order, same timing). Weaker forms of connascence are always preferred.",
            "Architecture styles: layered (n-tier), pipeline (filter and transform), microkernel (plug-in architecture), service-based, event-driven, space-based, orchestration-driven service-oriented, microservices. Each represents a specific set of trade-offs — no style is universally best.",
            "Monolithic vs. distributed architectures: monolithic styles (layered, pipeline, microkernel) are deployed as a single unit. Distributed styles (microservices, event-driven, service-based) are deployed as separate services. Distributed introduces fallacies of distributed computing — network unreliability, latency, bandwidth limits, topology changes.",
            "The eight fallacies of distributed computing (Peter Deutsch): the network is reliable; latency is zero; bandwidth is infinite; the network is secure; topology doesn't change; there is one administrator; transport cost is zero; the network is homogeneous. Every distributed architecture violates these assumptions in practice.",
            "Layered architecture: technical partitioning — presentation, business, persistence, database layers. Simple and familiar, but creates excessive coupling between layers and makes it hard to deploy parts independently. Works well for small, simple applications with constrained teams.",
            "Event-driven architecture: highly decoupled components that communicate via events. Two topologies: mediator (a central event mediator orchestrates a workflow) and broker (events are chained directly between publishers and subscribers). Excellent for scalability and decoupling; harder to test and debug.",
            "Microservices architecture: the most popular modern style for large distributed systems. Each service is bounded by a domain context, deployed independently, and communicates via network protocols. Operational complexity is extremely high — you need container orchestration, service mesh, distributed tracing, and API gateways.",
            "Service granularity in microservices: the hardest decision is how fine or coarse to make services. Drivers for granularity: service functionality (single-purpose), code volatility (frequently changed code in its own service), scalability (independently scalable), fault tolerance (isolated failure), security (different access requirements).",
            "Architecture fitness functions: objective integrity checks on architectural characteristics. Automated tests that verify your architecture doesn't drift from its intended state. A fitness function for cyclic dependencies verifies no cycles in the dependency graph. A fitness function for response time verifies p95 latency stays below 200ms.",
            "Evolutionary architecture: systems that change over time while maintaining architectural principles. Fitness functions operationalize architectural characteristics so they can be measured. Incremental change via CI/CD enables evolutionary architecture. Appropriate coupling — managed rather than minimized — enables safe change.",
            "The architect's role: architects are not removed from code. The best architects write code regularly to stay current, understand the implications of their decisions, and maintain technical credibility. Architects who don't code eventually make impractical decisions.",
            "Architecture decision records (ADRs): documenting why architectural decisions were made, not just what was decided. Context (the situation that prompted the decision), decision (what was chosen), status (proposed, accepted, deprecated), consequences (the trade-offs accepted). ADRs prevent re-litigating the same debates.",
            "Diagramming architecture: C4 model (Context, Container, Component, Code), Arc42, and UML are common frameworks. The most important thing is to make implicit architecture explicit and keep diagrams current. Diagrams as code (Mermaid, PlantUML, Structurizr) enable version-controlled, always-current documentation.",
            "Technical breadth vs. technical depth: developers optimize for technical depth (deep expertise in specific technologies). Architects need technical breadth (sufficient knowledge across many technologies to make informed trade-off decisions). The transition from developer to architect requires consciously expanding breadth.",
        ],
    },

    # ── 4. Software Architecture: The Hard Parts ───────────────────────────────
    {
        "title": "Software Architecture: The Hard Parts — Modern Trade-Off Analyses for Distributed Architectures",
        "author": "Neal Ford, Mark Richards, Pramod Sadalage, Zhamak Dehghani",
        "year": 2021,
        "memories": [
            "The central thesis of Software Architecture: The Hard Parts: there are no best practices in software architecture, only trade-offs. The job of the architect is to analyze trade-offs and help teams make the best decision for their specific situation.",
            "Decomposition patterns — how to split a monolith: component-based decomposition (extract logical components first, then services), tactical forking (duplicate the monolith, then whittle away), strangler fig (incrementally replace the old system with the new). Each has different risk profiles and operational characteristics.",
            "Service granularity disintegrators (forces that push toward smaller services): service functionality (single-purpose cohesion), code volatility (code that changes frequently belongs in its own service), scalability (services that need independent scaling), fault tolerance (isolating components that can fail), security (different trust boundaries).",
            "Service granularity integrators (forces that push toward larger services): database transactions (services that need ACID transactions should be together), data relationships (tightly related data belongs in one service), workflow and choreography (complex multi-service orchestration suggests consolidation), shared code (excessive code duplication suggests a larger service).",
            "Data ownership in distributed architectures: the hardest problem. Every service in a microservices architecture must own its own data — no shared databases. But many business processes span service boundaries. This creates a fundamental tension that must be managed through eventual consistency, sagas, or careful service boundary design.",
            "Distributed transactions — the saga pattern: a sequence of local transactions, each with a compensating transaction if a step fails. Choreography-based sagas use events; orchestration-based sagas use a central coordinator. Choreography is simpler but harder to reason about; orchestration provides visibility but coupling.",
            "The ACID vs. BASE trade-off: ACID (Atomicity, Consistency, Isolation, Durability) provides strong consistency but requires colocated data and limits scalability. BASE (Basically Available, Soft state, Eventually consistent) allows distributed data but requires accepting eventual consistency. Most microservices systems are BASE.",
            "Eventual consistency patterns: background synchronization (a background process reconciles data periodically), orchestrated request-based synchronization (a coordinator manages the synchronization workflow), event-based synchronization (services emit events that other services consume to stay in sync).",
            "Data decomposition patterns for databases: identify tables used by multiple services (shared tables are a monolith in disguise), separate table schemas per service, break joint ownership tables into service-specific versions, use replication for read access to other services' data.",
            "Contract coupling in distributed architectures: strict contracts (both consumer and provider must agree on the exact structure) vs. loose contracts (consumers only parse what they need). Consumer-driven contract testing (Pact) enables loose coupling with automated verification.",
            "Stamp coupling: the anti-pattern of passing a large data structure when only a small part is needed, creating coupling to the full structure. Solutions: create field selectors (only pass needed fields), implement a request-reply pattern with specific responses, or use value objects sized appropriately.",
            "Reuse and microservices: code reuse in distributed architectures is much harder than in monolithic architectures. Libraries create coupling through shared deployment. Shared services create operational coupling. Sidecars (service mesh) provide reuse without deployment coupling. The decision must weigh code consistency vs. coupling risk.",
            "Shared domain functionality: some functionality is genuinely shared across many services (audit logging, rate limiting, authentication). Options: shared library (deployment coupling), shared service (operational coupling), sidecar (infrastructure complexity), service template (code duplication with consistency). Each is a trade-off.",
            "Orchestration vs. choreography: orchestration uses a central coordinator (workflow engine) that explicitly controls the sequence of steps. Choreography uses events — each service reacts to events and emits new events. Orchestration provides visibility and error handling; choreography provides decoupling and scalability.",
            "Workflow state management: when a workflow spans multiple services, where does the state live? Options: the orchestrator (centralized, tight coupling), a dedicated state store (decentralized but adds a new service), event-carried state transfer (state embedded in events). The choice affects failure recovery and observability.",
            "Transactional outbox pattern: a service that needs to update its database AND emit an event atomically writes the event to an 'outbox' table in the same database transaction. A separate process reads from the outbox and publishes events. Guarantees at-least-once delivery without distributed transactions.",
            "Decomposing the database first vs. service first: when breaking apart a monolith, splitting the database first reveals data ownership issues before you commit to service boundaries. But you can also start with service boundaries and defer database splitting. The strangler fig pattern lets you do it incrementally.",
            "Anti-patterns in distributed architectures: the distributed monolith (microservices that are actually tightly coupled and must be deployed together), logging without correlation IDs (impossible to trace a request across services), synchronous chains (long chains of synchronous service calls multiply latency and failure probability), shared databases.",
            "Sidecar and service mesh patterns: a sidecar is a separate container deployed alongside each service instance to handle cross-cutting concerns (logging, metrics, security, circuit breaking) without coupling to the service code. A service mesh (Istio, Linkerd) provides a uniform control plane over all sidecars.",
            "Fitness functions for distributed architecture: contract tests verify service API compatibility, chaos engineering tests resilience, performance tests verify latency budgets, data consistency tests verify eventual consistency converges, circuit breaker tests verify fault tolerance. These are runnable architectural assertions.",
        ],
    },

    # ── 5. Head First Software Architecture ───────────────────────────────────
    {
        "title": "Head First Software Architecture",
        "author": "Raju Satin, Mark Richards, Neal Ford",
        "year": 2024,
        "memories": [
            "Head First Software Architecture introduces architecture thinking as a practice: the ability to zoom out from code to see systems, trade-offs, and constraints. Not a set of rules — a way of thinking that gets better with deliberate practice.",
            "The four core abilities of a software architect: make architectural decisions (choose between options with unclear right answers), continually analyze the architecture (is it still fit for purpose?), keep current with latest trends and technology, and have interpersonal skills (architects must communicate, negotiate, and lead).",
            "Why architecture is hard: architectural decisions are typically irreversible or very expensive to reverse. Architects work with incomplete information and must make decisions anyway. The full consequences of architectural decisions often only emerge months or years later.",
            "Architecture quantum: an independently deployable artifact with high functional cohesion, high static coupling, and synchronous dynamic coupling. In a monolith, there is one architecture quantum. In microservices, each service is a separate architecture quantum. Understanding architecture quanta helps identify service boundaries.",
            "The trade-off triangle: you can generally optimize for two of three qualities — fast, good, and cheap. In software architecture, similar trade-offs exist between scalability, consistency, and availability. Recognizing and articulating these trade-offs is a core architectural skill.",
            "Coupling and cohesion at the architectural level: afferent coupling (how many things depend on a component) and efferent coupling (how many things a component depends on). High afferent coupling means a component is hard to change without breaking other things. High efferent coupling means a component is hard to understand and test.",
            "Anti-pattern: the Big Ball of Mud — a system with no discernible architecture. Code is added wherever it's convenient. Changes are risky because the implications are unknown. It's the natural state software decays toward without deliberate architectural effort.",
            "Layered architecture pitfalls: the sinkhole anti-pattern (requests pass through layers without adding value — just delegating), tight coupling between adjacent layers making it hard to change one without the other, and performance overhead from unnecessary processing in each layer.",
            "Event-driven architecture — the mediator topology vs. broker topology: in mediator, a central orchestrator routes events to appropriate handlers and maintains state. In broker, there is no central coordinator — each event handler processes events and emits new events. Mediator is easier to debug; broker is more decoupled.",
            "The CAP theorem in practice: most databases choose between CP (consistent and partition-tolerant, sacrificing availability — ZooKeeper, HBase) and AP (available and partition-tolerant, sacrificing consistency — CouchDB, Cassandra). RDBMS systems typically choose CA, assuming no network partitions in a single-datacenter deployment.",
            "Space-based architecture: designed for massive scalability. In-memory data grids provide the shared state that normally lives in a database. Processing units (stateless compute instances) are scaled out on demand. Asynchronous messaging to a persistent store ensures durability. Excellent elasticity; complex to operate.",
            "Microkernel architecture (plug-in pattern): a core system provides minimal functionality; plug-ins add application-specific logic. The core system manages the plug-in registry and handles communication between plug-ins. Excellent for product-based software that must be customized for each client.",
            "Architecture decision catalogs: maintaining a catalog of common architectural decisions with their pros, cons, and when-to-use guidance. This enables consistent decision-making across teams and gives new architects a reference for common patterns.",
            "The architecture kata exercise: a structured way to practice architectural thinking. Given a short problem statement, define the implicit characteristics, draw a component diagram, and be prepared to explain every decision. Katas build architectural intuition the same way coding katas build coding intuition.",
            "Behavioral driven architecture: design principles that guide architectural decisions. SOLID principles apply at the component level (not just the class level). The Single Responsibility Principle at the component level means each component has one reason to change.",
            "Risk assessment in architecture: architects must assess two dimensions — likelihood of failure and impact of failure. High-likelihood, high-impact risks require mitigation. Low-likelihood, low-impact risks are accepted. The risk matrix guides how much architectural investment is warranted.",
        ],
    },

    # ── 6. System Design Interview: An Insider's Guide ─────────────────────────
    {
        "title": "System Design Interview: An Insider's Guide",
        "author": "Alex Xu",
        "year": 2020,
        "memories": [
            "The system design interview framework: Step 1 — understand the problem and establish design scope (ask clarifying questions, gather requirements). Step 2 — propose high-level design (get buy-in from interviewer). Step 3 — design deep dive (focus on the components that matter most). Step 4 — wrap up (identify bottlenecks, discuss trade-offs).",
            "Back-of-envelope estimation: essential skill for system design. Assume 2.5 billion users, 1 billion DAU, 10% post daily, 10 posts/day = 1 billion posts/day = ~12,000 writes/second. Storage: 1 billion posts × 1KB = 1TB/day = 365TB/year. These rough estimates guide architectural decisions.",
            "Designing a rate limiter: token bucket, leaking bucket, fixed window counter, sliding window log, and sliding window counter algorithms. Redis is the standard implementation. Rate limiting at the API gateway layer handles cross-service rate limiting. Per-user limits, per-IP limits, and per-endpoint limits serve different use cases.",
            "Designing a URL shortener: hash function (MD5 or SHA-256) + base62 encoding generates a 7-character short URL. Concerns: collision handling, custom short URLs, analytics, expiration. Database: a simple key-value store (short URL → long URL). Cache the most popular URLs. NoSQL (Cassandra) for scale.",
            "Designing a web crawler: a component that downloads web pages, extracts URLs, and repeats. Key challenges: scale (billions of URLs), politeness (don't overwhelm servers with requests), freshness (re-crawl pages that change), storage (petabytes of content). URL frontier (priority queue), DNS resolver cache, and distributed parsing are the main components.",
            "Designing a notification system: push notifications (iOS APNs, Android FCM), SMS (Twilio, Nexmo), and email (Sendgrid). Key components: notification server, message queue (one per notification type), workers, third-party services. At-least-once delivery with idempotent consumers. Rate limiting per user prevents spam.",
            "Designing a news feed system: fanout on write (pre-compute news feeds when a post is created) vs. fanout on read (compute news feed on demand). Fanout on write is fast for readers but expensive for celebrities with millions of followers. Hybrid approach: fanout on write for regular users, fanout on read for celebrity accounts.",
            "Designing a chat system: 1-on-1 vs. group chat. Long polling (clients repeatedly poll server) vs. WebSocket (full-duplex persistent connection). WebSocket is standard for chat. Message storage: NoSQL (HBase, Cassandra) for chat history, optimized for time-range queries. Message ID generation: Snowflake ID for global uniqueness.",
            "Designing a search autocomplete system: prefix-based search using a trie data structure. Challenge: trie is too large for one server. Solution: shard by first character, cache top suggestions at each trie node. Cache the trie to avoid cold-start latency. Filtering and ranking: filter inappropriate content, rank by query frequency.",
            "Designing YouTube / video streaming: upload flow (chunked upload, transcoding to multiple resolutions, CDN storage) vs. streaming flow (adaptive bitrate streaming, CDN delivery). Video transcoding is compute-intensive — use a dedicated transcoding service. Metadata stored in a relational database; video content on a CDN.",
            "Designing Google Drive / cloud file storage: client sync (conflict resolution, delta sync), metadata service (file tree structure), block storage (files split into blocks for deduplication). Strong consistency for metadata; eventual consistency acceptable for file blocks. Deduplication at the block level saves significant storage.",
            "Horizontal scaling: add more servers. Vertical scaling: add more resources to existing servers. Stateless web tier (session data in a shared data store) enables horizontal scaling. Database replication (master-slave) separates reads from writes. Cache (Redis, Memcached) reduces database load. CDN serves static assets globally.",
            "Database choices in system design: relational (MySQL, PostgreSQL) for structured data, complex queries, ACID transactions. NoSQL (Cassandra, MongoDB, DynamoDB) for unstructured data, high write throughput, horizontal scalability. In-memory (Redis) for caching, sessions, real-time analytics. Search (Elasticsearch) for full-text search.",
            "Consistent hashing: a technique for distributing data across nodes such that when nodes are added or removed, only a small fraction of keys need to be remapped. Use virtual nodes to account for non-uniform hash distribution. Standard in distributed caches (Memcached), DHTs, and load balancers.",
            "The CAP theorem applied to design interviews: if the interviewer asks whether your system should be consistent or available during a partition, you must know the trade-off and make an explicit choice. For financial systems: consistency. For social media: availability. State your choice and justify it.",
            "Sharding strategies: horizontal sharding (rows distributed across shards by key), vertical sharding (columns or tables distributed by function). Celebrity problem: a single shard that receives a disproportionate share of traffic. Solution: dedicate a shard to celebrity data or shard celebrity data with a different key.",
            "Heartbeat and failure detection: distributed systems need a way to detect when nodes have failed. Heartbeat (each node sends a heartbeat message periodically; missing heartbeats indicate failure) and gossip protocol (each node maintains a list of node statuses and shares it with random peers, spreading information in O(log N) rounds).",
            "Message queues in distributed systems: Kafka, RabbitMQ, and SQS decouple producers from consumers, buffer load spikes, and enable retry logic. Topics partition messages for parallelism. Consumer groups allow multiple consumers to process a topic in parallel. At-least-once vs. exactly-once delivery semantics matter for financial systems.",
            "Designing a distributed ID generator: auto-increment ID doesn't work across databases in a distributed system. Approaches: UUID (simple but not sortable, 128-bit), Twitter Snowflake (64-bit: timestamp + datacenter ID + machine ID + sequence number), ticket server (centralized ID generation — single point of failure). Snowflake is the industry standard.",
        ],
    },

    # ── 7. Software Engineering at Google ─────────────────────────────────────
    {
        "title": "Software Engineering at Google: Lessons Learned from Programming Over Time",
        "author": "Titus Winters, Tom Manshreck, Hyrum Wright",
        "year": 2020,
        "memories": [
            "Google's definition of software engineering: programming integrated over time. Code that doesn't need to change is just programming. Software engineering includes all the practices required to support a program over a long, possibly infinite lifetime. This reframes all engineering decisions around time.",
            "Hyrum's Law: with a sufficient number of users of an API, it does not matter what you promise in the contract; all observable behaviors of your system will be depended upon by somebody. Corollary: any change to a system with a large user base will break someone. This makes backwards-compatible API evolution extremely difficult at scale.",
            "The three types of tradeoffs Google engineers make: time versus resource tradeoffs (use more CPU or memory to save engineer time), correctness versus complexity tradeoffs (a simpler but slightly incorrect solution may be better than a complex but correct one), and maintainability versus performance tradeoffs.",
            "Google's rule of thumb for decisions: make the decision that serves the most engineers over the longest time. This explicitly encodes the time dimension into engineering decision-making and prioritizes the collective over the individual.",
            "Style guides and consistency: Google enforces a single style guide across all code in a given language. The value isn't in any specific rule but in having one rule, consistently applied. Consistency enables automated tooling, makes code reviews faster, and reduces cognitive overhead when switching between projects.",
            "Code review at Google: every change to production code requires at least one LGTM (approval) from a code owner. The code review is primarily about correctness, maintainability, and knowledge transfer — not about personal style. Comments should be actionable and specific. Reviews should be completed within 24 hours.",
            "Technical debt at Google: Google explicitly tracks 'toil' — repetitive, automatable work that doesn't add lasting value. Site Reliability Engineers (SREs) have a policy that no more than 50% of their time can be spent on toil. Anything above that triggers automation work to eliminate the toil source.",
            "The role of testing: Google invests heavily in testing infrastructure. Unit tests, integration tests, end-to-end tests, and property-based tests are all used. The key insight: tests are code and must be maintained. Flaky tests (tests that fail intermittently) are treated as bugs and fixed immediately — flakiness erodes trust.",
            "Test size vs. test scope: Google defines tests by size (small, medium, large) rather than by conventional categories (unit, integration, system). Small tests are hermetic — they run entirely in memory with no external dependencies. Medium tests can use local databases. Large tests can use production services. Size determines parallelism and speed.",
            "Deprecation at Google: adding new things is easy; removing old things is hard. Google's approach: make deprecation a first-class engineering activity. Announce deprecation clearly, provide a migration path, measure adoption, and eventually delete. 'Dead code is live bugs' — unmaintained code accumulates vulnerabilities and misunderstandings.",
            "The value of trunk-based development: Google uses a single giant monorepo (1 billion lines of code, 25 thousand engineers) with trunk-based development. All engineers commit to the main branch daily. Feature flags manage unreleased features. Short-lived branches reduce merge conflicts. Build artifacts are cached globally.",
            "Bazel and build systems: Google's build system (open-sourced as Bazel) enables hermetic, reproducible builds across 25 million files. Every build target explicitly declares its dependencies — no implicit dependencies. This enables remote execution, caching, and parallelism at massive scale.",
            "Continuous integration at Google: every commit triggers automated tests. The test suite must run in minutes, not hours, to maintain developer velocity. Sharding tests across thousands of machines in Google's distributed testing infrastructure is what makes this possible.",
            "Code ownership and stewardship: every file in Google's codebase has a named owner (or team). Owners are responsible for reviewing changes, maintaining quality, and deprecating code when appropriate. Without ownership, code becomes everyone's problem and no one's responsibility — it decays.",
            "Documentation at Google: docs are maintained alongside code. Every public API has documentation. The docs-as-code philosophy (write docs in Markdown, store in version control, review like code) keeps documentation current. Google's g3doc system renders documentation from source files.",
            "Large-scale changes (LSC): Google regularly makes changes across all 1 billion lines of code — renaming a function used 10,000 times, updating a deprecated API, applying a security fix universally. The tooling for LSC (Rosie, the LSC system) enables atomic, reviewed, tested changes across the entire codebase.",
            "The 1% rule for reliability: Google's SREs target 99.99% availability for most services. That's 52 minutes of downtime per year. Achieving this requires: error budgets (tolerate this much downtime), SLOs (service level objectives), SLIs (measurements of reliability), and automated failover. When you exceed the error budget, you stop feature work and fix reliability.",
            "Oncall and incident response: Google rotates oncall responsibilities. Postmortems are blameless — they focus on systemic causes, not individual mistakes. Postmortems are written within 24-48 hours of an incident and shared broadly. Corrective actions are assigned and tracked. This culture of continuous improvement is how reliability improves.",
            "The relationship between scale and simplicity: at Google's scale, simple solutions often outperform clever ones. A hash table in a single process outperforms a distributed key-value store for small datasets. The coordination overhead of distributed systems is expensive. Choose distributed architectures only when scale requires it.",
            "Engineering culture: Google's engineering culture values data over opinion, experimentation over debate, and distributed authority over central control. Changes are defended with data. A/B tests settle disputes about user-facing decisions. This empirical culture scales better than opinion-based culture as organizations grow.",
        ],
    },
]


def main():
    global _current_book, _total_memories

    log("="*60)
    log(f"Software Architecture Books Ingest")
    log(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Books: {len(BOOKS)}")
    total_planned = sum(len(b["memories"]) for b in BOOKS)
    log(f"Total memories to store: {total_planned}")
    log("="*60)

    post_notify(
        f":books: *Software Architecture Books — Ingest Starting*\n"
        f"• 7 books: Clean Architecture, DDIA, Fundamentals, Hard Parts, Head First, System Design Interview, SWE at Google\n"
        f"• Memories to store: *{total_planned}*\n"
        f"• Source: `{SOURCE}`\n"
        f"• Progress updates every 5 minutes"
    )

    for book in BOOKS:
        ingest_book(
            title=book["title"],
            author=book["author"],
            year=book["year"],
            memories=book["memories"],
        )

    elapsed = int(time.time() - _start_time)
    m, s = divmod(elapsed, 60)

    log("\n" + "="*60)
    log(f"Ingest complete!")
    log(f"Total memories stored: {_total_memories}")
    log(f"Total time: {m}m {s}s")
    log("="*60)

    post_notify(
        f":white_check_mark: *Software Architecture Books — Ingest Complete*\n"
        f"• Memories stored: *{_total_memories}*\n"
        f"• Source: `{SOURCE}`\n"
        f"• Total time: {m}m {s}s\n"
        f"• Recall now available for architecture topics: Clean Architecture, microservices, distributed systems, data-intensive applications, system design, Google SWE practices"
    )


if __name__ == "__main__":
    main()
