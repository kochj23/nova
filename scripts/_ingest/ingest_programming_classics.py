#!/usr/bin/env python3
"""
ingest_programming_classics.py — Ingest canonical programming books into Nova's memory.

Books covered:
  1.  The Pragmatic Programmer — Hunt & Thomas
  2.  Clean Code — Robert C. Martin
  3.  Design Patterns (GoF) — Gamma, Helm, Johnson, Vlissides
  4.  Code Complete — Steve McConnell
  5.  Refactoring — Martin Fowler
  6.  The Mythical Man-Month — Frederick P. Brooks Jr.
  7.  Head First Design Patterns — Freeman & Freeman
  8.  Working Effectively with Legacy Code — Michael Feathers
  9.  Domain-Driven Design — Eric Evans
  10. Effective Java — Joshua Bloch
  11. Grokking Algorithms — Aditya Bhargava
  12. Programming Pearls — Jon Bentley
  13. Build a Large Language Model (From Scratch) — Sebastian Raschka

Posts progress to #nova-notifications every 5 minutes.
Source: "programming_books"

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

MEMORY_SERVER = "http://192.168.1.6:18790"
SOURCE        = "programming_books"
BATCH_DELAY   = 0.3
NOTIFY_EVERY  = 300

LOG_FILE = Path.home() / ".openclaw/logs/ingest_programming_classics.log"

_last_notify = time.time()
_total_memories = 0
_current_book = ""
_start_time = time.time()


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def post_notify(msg):
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)


def maybe_notify(force=False):
    global _last_notify
    now = time.time()
    if force or (now - _last_notify) >= NOTIFY_EVERY:
        elapsed = int(now - _start_time)
        m, s = divmod(elapsed, 60)
        post_notify(
            f":books: *Programming Classics — Ingest Update*\n"
            f"• Current: *{_current_book}*\n"
            f"• Stored: *{_total_memories}*\n"
            f"• Elapsed: {m}m {s}s"
        )
        _last_notify = now


def remember(text, metadata=None):
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
        log(f"  ERR: {e}")
    time.sleep(BATCH_DELAY)
    maybe_notify()


def ingest_book(title, author, year, memories):
    global _current_book
    _current_book = f"{title} ({author})"
    log(f"\n{'='*60}\n{title}\n{author} · {year} · {len(memories)} memories")
    meta = {"book": title, "author": author, "year": year}
    for i, mem in enumerate(memories, 1):
        remember(mem, meta)
        if i % 10 == 0:
            log(f"  {i}/{len(memories)}...")
    log(f"  Done: {len(memories)} stored")


BOOKS = [

    {
        "title": "The Pragmatic Programmer: From Journeyman to Master",
        "author": "Andy Hunt and David Thomas",
        "year": 1999,
        "memories": [
            "The Pragmatic Programmer's central metaphor: software rot. Like a broken window in a building that accelerates vandalism, one bad piece of code — left unrepaired — signals that no one cares. Fix bad code immediately, or at least board it up. Don't leave broken windows.",
            "DRY — Don't Repeat Yourself: every piece of knowledge must have a single, unambiguous, authoritative representation within a system. Not just code duplication — duplicated logic, duplicated documentation, duplicated data structure definitions. DRY violations are maintenance nightmares.",
            "Orthogonality: two things are orthogonal if changing one doesn't affect the other. Design orthogonal components — a change to the database shouldn't require changes to the UI. The more components are decoupled, the more orthogonal the system.",
            "Tracer bullets: build thin slices of complete functionality end-to-end first, rather than building each layer completely. Like tracer bullets that show where actual bullets will go, tracer code shows if the pieces fit together. Different from prototyping — tracer code is production code that grows.",
            "The rubber duck debugging technique: explain your code, line by line, to an inanimate object. The act of articulating the problem out loud often reveals the bug. You see what the code does rather than what you think it does.",
            "Programming by coincidence: developers who don't know why their code works are programming by coincidence. They depend on undocumented behavior, rely on the order of side effects, and can't reason about edge cases. Know why your code works.",
            "Algorithm complexity: every developer should understand Big-O notation. Knowing that your nested loop is O(n²) helps you anticipate performance problems before they appear in production. Hunt and Thomas say developers must 'estimate the order of your algorithms.'",
            "Refactoring: Hunt and Thomas define refactoring as disciplined restructuring of existing code to improve its internal structure without changing external behavior. Refactor early and often. The longer you wait, the more expensive it becomes.",
            "Design by contract: define the preconditions, postconditions, and invariants of every module. Preconditions are what must be true before calling a routine. Postconditions are what the routine guarantees when it finishes. Invariants are conditions that must remain true throughout.",
            "The law of Demeter (principle of least knowledge): a method should only call methods on its own class, parameters passed to it, objects it creates, and directly held component objects. Don't talk to strangers — don't chain method calls across objects you don't own.",
            "Prototyping: build throwaway code to explore unknowns. Prototypes are for answering a specific question — not for shipping. Hunt and Thomas emphasize: make sure everyone knows a prototype is disposable. Prototypes that accidentally become production code are common disasters.",
            "Pragmatic pragmatism: be realistic about what you can achieve. Hunt and Thomas emphasize that 'good enough' software shipped today is often better than 'perfect' software delivered never. Know when to stop — perfectionism is a trap.",
            "Power of plain text: store knowledge in plain text. Plain text doesn't become obsolete. It can be version-controlled, grepped, parsed by any tool. Binary formats tie you to specific applications. Human-readable is machine-readable.",
            "Shell power: learn your shell deeply. Hunt and Thomas argue that developers who don't use shell commands for automation are like craftspeople who refuse to use their best tools. Automate everything repetitive.",
            "Ubiquitous automation: automate anything you do more than twice. Continuous integration, automated testing, deployment pipelines, database migrations — automate them all. The pragmatic programmer is also a pragmatic automator.",
            "The broken windows theory in software teams: one bad design decision that leadership allows to stand signals that such decisions are acceptable. This makes the next bad decision easier. Maintain standards ruthlessly at the team level.",
            "Career investment in your knowledge portfolio: treat your skills like a financial portfolio. Invest regularly (learn constantly), diversify (don't be a single-language developer), balance risk (learn emerging technologies but keep fundamentals strong), review regularly (prune skills that are no longer valuable).",
        ],
    },

    {
        "title": "Clean Code: A Handbook of Agile Software Craftsmanship",
        "author": "Robert C. Martin",
        "year": 2008,
        "memories": [
            "Clean Code's foundational premise: the only valid measurement of code quality is WTFs/minute during code review. Good code is code that other developers can read, understand, and modify without frustration. Writing clean code is not a luxury — it's the baseline of professional craftsmanship.",
            "Meaningful names: use intention-revealing names. The name of a variable, function, or class should tell you why it exists, what it does, and how it is used. If a name requires a comment, it isn't a good name. Names like 'd' or 'temp' fail this test.",
            "Functions should do one thing: functions should do one thing, they should do it well, they should do it only. The smaller a function is, the easier it is to name, test, and understand. Functions that do multiple things should be broken apart.",
            "Function arguments: the ideal number of arguments for a function is zero (niladic). Next best is one (monadic), followed closely by two (dyadic). Three arguments (triadic) should be avoided where possible. More than three requires very special justification — and should never be used.",
            "Don't repeat yourself (DRY) in Clean Code: duplication is the root of all evil in software. Every time you see duplication, it represents a missed opportunity for abstraction. Uncle Bob traces the history of software development as a history of strategies for eliminating duplication.",
            "Comments as code smell: clean code is self-documenting. Comments are typically a failure to express intent in code. The best comment is no comment at all — refactor the code to explain itself. Acceptable comments: legal notices, explanation of intent, warnings, and TODO markers.",
            "The newspaper metaphor for code structure: well-structured code reads like a newspaper. High-level concepts at the top, details at the bottom. Functions called by a function should appear just below it. The reader should be able to understand the structure by scanning from top to bottom.",
            "Error handling with exceptions: use exceptions rather than return codes. Error codes force the caller to check them (and many don't). Exceptions separate the happy path from error handling, making both clearer. Don't use exceptions for control flow.",
            "Don't return null: Uncle Bob argues that returning null from methods is bad practice — it forces callers to check for null everywhere. Return an empty list rather than null, or use the Null Object pattern. Null pointer exceptions are a plague caused by returning and passing null.",
            "The Single Responsibility Principle at the class level: a class should have only one reason to change. If you can think of more than one reason to change a class, it has more than one responsibility. This is the most important principle in object-oriented design.",
            "The Open/Closed Principle: software entities should be open for extension but closed for modification. New behavior should be added by adding new code, not by modifying existing code. Achieved through abstraction — program to interfaces, not implementations.",
            "Test-Driven Development (TDD) three laws: First, you may not write production code until you have written a failing unit test. Second, you may not write more of a unit test than is sufficient to fail. Third, you may not write more production code than is sufficient to pass the failing test.",
            "The F.I.R.S.T. principles for clean tests: Fast (tests must run quickly), Independent (tests should not depend on each other), Repeatable (tests should be runnable in any environment), Self-Validating (tests should pass or fail — no manual inspection), Timely (tests should be written just before the production code).",
            "Systems and architecture in Clean Code: cities are built with separation of concerns — water department, power department, police department. Software systems should be built the same way. Keep startup (constructing objects) separate from use (runtime logic). Use factories, dependency injection frameworks.",
            "Emergent design: Kent Beck's four rules of simple design. The system runs all the tests. The system contains no duplication. The system expresses the intent of the programmer. The system minimizes the number of classes and methods. These rules, in priority order, lead to simple, clean design.",
        ],
    },

    {
        "title": "Design Patterns: Elements of Reusable Object-Oriented Software",
        "author": "Erich Gamma, Richard Helm, Ralph Johnson, John Vlissides (Gang of Four)",
        "year": 1994,
        "memories": [
            "The Gang of Four book introduced the concept of design patterns to software: recurring solutions to commonly occurring problems in software design. A pattern is not a finished design — it's a template describing how to solve a problem in many different situations.",
            "Creational patterns — how objects are created: Factory Method (defer instantiation to subclasses), Abstract Factory (create families of related objects), Builder (construct complex objects step by step), Prototype (clone objects), Singleton (ensure one instance). Creational patterns hide the specifics of how objects are created.",
            "The Singleton pattern: ensures a class has only one instance and provides a global access point to it. Often considered an anti-pattern today because it introduces global state, makes testing difficult, and violates the Single Responsibility Principle. Use with caution.",
            "Structural patterns — how classes and objects are composed: Adapter (converts interface of a class into another interface), Bridge (separates abstraction from implementation), Composite (composes objects into tree structures), Decorator (adds responsibilities to objects dynamically), Facade (unified interface to a set of interfaces), Flyweight (shared objects to support large numbers efficiently), Proxy (surrogate for another object).",
            "The Decorator pattern: attaches additional responsibilities to an object dynamically. Decorators provide a flexible alternative to subclassing for extending functionality. Classic example: Java's I/O streams, where BufferedReader wraps FileReader wraps File. New behavior added without modifying existing classes.",
            "Behavioral patterns — how objects interact: Chain of Responsibility, Command, Interpreter, Iterator, Mediator, Memento, Observer, State, Strategy, Template Method, Visitor. Behavioral patterns define how objects communicate and assign responsibility.",
            "The Observer pattern: defines a one-to-many dependency so that when one object changes state, all its dependents are notified and updated automatically. The foundation of event-driven programming, MVC frameworks, and reactive programming. Publisher-subscriber is a distributed version of Observer.",
            "The Strategy pattern: defines a family of algorithms, encapsulates each one, and makes them interchangeable. Strategy lets the algorithm vary independently from clients that use it. Classic example: sorting algorithms, payment methods, compression algorithms. Replaces conditionals with polymorphism.",
            "The Template Method pattern: defines the skeleton of an algorithm in a base class, deferring some steps to subclasses. Template Method lets subclasses redefine certain steps of an algorithm without changing its structure. The Hollywood Principle: 'Don't call us, we'll call you.'",
            "The Command pattern: encapsulates a request as an object, allowing you to parameterize clients with different requests, queue or log requests, and support undoable operations. Used for: undo/redo, transaction logging, macro recording, GUI buttons and menu items.",
            "The Composite pattern: composes objects into tree structures to represent part-whole hierarchies. Clients treat individual objects and compositions of objects uniformly. Classic example: file system (files and directories), UI component hierarchies, expression trees.",
            "The Factory Method pattern: defines an interface for creating an object, but lets subclasses decide which class to instantiate. Defers instantiation to subclasses. Use when a class can't anticipate the class of objects it needs to create.",
            "The Facade pattern: provides a simplified interface to a complex subsystem. A facade doesn't encapsulate the subsystem — it provides a simplified entry point while allowing direct access to the subsystem for clients that need more control. Used in API design and service layers.",
            "Principles underlying the GoF patterns: program to an interface, not an implementation. Favor object composition over class inheritance. The patterns embody these two principles in different ways. Inheritance is rigid and establishes a tight coupling between classes.",
            "Pattern language: GoF introduced the idea that patterns form a language. Patterns reference other patterns — a Facade might use a Factory, which might use a Singleton. Learning the vocabulary of patterns enables architects and developers to communicate precisely about structure.",
        ],
    },

    {
        "title": "Code Complete: A Practical Handbook of Software Construction",
        "author": "Steve McConnell",
        "year": 1993,
        "memories": [
            "Code Complete's central metaphor: software construction is more like building a house than engineering a bridge. You need blueprints (architecture), but construction details matter enormously. Good construction practices are independent of what paradigm, language, or methodology you use.",
            "The importance of prerequisites: the most common source of programming errors is flawed or missing requirements. Before writing a line of code, understand what you're building, why, and for whom. Fixing a requirements error after code is written costs 10-100x more than catching it before coding begins.",
            "Design levels: software design operates at multiple levels simultaneously — software system, division into subsystems, division into classes, division into routines, internal routine design. Each level requires different design techniques and produces different kinds of decisions.",
            "High-quality routines (functions/methods): routines should do one thing and do it well. The ideal routine length is hard to specify — it depends on complexity. Routines over 200 lines are usually problematic. Cohesion (routines that do one related thing) is more important than length alone.",
            "Defensive programming: code defensively against misuse. Check the values of input parameters. Use assertions to document assumptions. Handle boundary conditions explicitly. Don't assume the input will always be what you expect — treat every external input as potentially hostile.",
            "The pseudocode programming process (PPP): write pseudocode first, then translate to actual code. Start high-level, refine iteratively. This technique catches design problems before they become code problems. McConnell argues that writing pseudocode for complex routines saves more time than it takes.",
            "Variables: use the variable for one purpose only. Don't use a variable for two different meanings even if the meanings are at different times. Each variable should have a single role that is obvious from its name. Variables used for multiple purposes become sources of subtle bugs.",
            "Naming conventions: good names describe the entity fully and accurately. Use problem-domain terminology. For variables, include what, when/where if applicable. A good name is a short sentence. Bad names like 'x', 'temp', 'data', and 'handle' tell you nothing.",
            "Control structures: complexity in conditionals is a major source of bugs. Deep nesting (more than three levels) makes code hard to understand. Flatten conditionals by reversing conditions and returning early. Convert complex boolean expressions to named variables with meaningful names.",
            "Table-driven methods: a powerful alternative to complex logic. Instead of a series of if/else or switch statements, look up the answer in a table. Tables are often much simpler than the equivalent logic, easier to understand, and easier to modify.",
            "Debugging: McConnell treats debugging as a skill that can be systematically improved. Scientific debugging: stabilize the error (reproduce it reliably), locate the source (narrow down where), fix the defect, test the fix. Most importantly: understand why the bug occurred before fixing it.",
            "Testing: unit testing is the developer's responsibility. Developers should write tests as they write code. Tests serve as executable specifications. The cost of finding and fixing defects rises dramatically the later in development they are found — from $1 at requirements to $100 at maintenance.",
            "Software construction quality: code is read far more often than it is written. Code should be written for the reader, not the writer. Clarity is the primary virtue of code. Efficiency, cleverness, and performance are secondary to clarity except in specifically identified hot spots.",
            "Managing complexity: the most important technical topic in software development. Humans can hold about 7 plus or minus 2 things in working memory. Good software design manages this by creating layers of abstraction that let you think about one layer without knowing about the others.",
            "Software quality: quality is not a property you add at the end — it must be built in from the start. The external quality attributes visible to users (correctness, usability, efficiency, reliability) depend on internal quality attributes (maintainability, flexibility, portability, reusability, readability, testability).",
        ],
    },

    {
        "title": "Refactoring: Improving the Design of Existing Code",
        "author": "Martin Fowler",
        "year": 1999,
        "memories": [
            "Refactoring defined: a disciplined technique for restructuring an existing body of code, altering its internal structure without changing its external behavior. The two key points: internal structure only, and external behavior preserved. This requires tests to verify behavior is preserved.",
            "Code smells: Fowler and Kent Beck's vocabulary for patterns in code that indicate a possible need for refactoring. Not bugs — they may work fine — but they hint at problems in design. Naming them gives teams a shared vocabulary for discussing code quality issues.",
            "The most important code smells: Long Method (method too long to understand), Large Class (class doing too much), Long Parameter List (too many parameters), Divergent Change (one class changes for many different reasons), Shotgun Surgery (one change forces changes in many classes), Feature Envy (method uses data from another class more than its own).",
            "More critical code smells: Data Clumps (same groups of data that belong together), Primitive Obsession (using primitives instead of small objects for domain concepts), Switch Statements (often better replaced with polymorphism), Parallel Inheritance Hierarchies, Lazy Class, Speculative Generality, Temporary Field, Message Chains.",
            "Extract Method: the most common and most useful refactoring. When a fragment of code can be grouped together, move it to a new method with a name that explains its purpose. This is the most powerful refactoring for improving readability and eliminating duplication.",
            "Move Method: if a method uses more features of another class than the class it's in, create a new method in the class it most uses. This improves cohesion and reduces coupling. Often follows Feature Envy code smell identification.",
            "Replace Conditional with Polymorphism: when you have a conditional that chooses different behavior based on the type of an object, replace each branch with a polymorphic method. Polymorphism is cleaner than switch statements, easier to extend, and eliminates the need to modify existing code when adding new types.",
            "Introduce Parameter Object: when a group of parameters naturally goes together, replace them with an object. This reduces the length of parameter lists and groups related data. Often reveals hidden concepts that become domain objects.",
            "Replace Magic Number with Symbolic Constant: magic numbers in code are maintainability hazards. Someone has to figure out what 86400 means (seconds per day). Replace magic numbers with named constants that document their meaning and make global changes easy.",
            "The refactoring workflow: don't refactor and add features simultaneously. Before refactoring, ensure you have tests. Refactor in small, safe steps. Run tests after each step. If you break tests, you've changed behavior — revert and try again. Small, safe steps are the key to successful refactoring.",
            "When not to refactor: Fowler says sometimes it's more practical to rewrite than to refactor. If code is so messy that refactoring would cost more than rewriting, rewrite. If you're close to a deadline, defer refactoring. But always plan to refactor — technical debt accumulates interest.",
            "Refactoring and testing: refactoring without tests is extremely risky. Tests are what allow you to safely change code. If you don't have tests, write them before refactoring. The difficulty of writing tests for existing code reveals design problems — hard to test means poorly designed.",
            "The two hats metaphor: when developing software, you wear two different hats at different times — adding function and refactoring. When adding function, don't change existing code; just add new tests and make them work. When refactoring, make no new tests (except when you find a case you missed); only restructure the code.",
            "Refactoring in databases: schema changes are the hardest part of refactoring a data-intensive system. Fowler advocates for evolutionary database design — making small, incremental schema changes supported by migration scripts. Version-control your schema alongside your code.",
            "Replace Temp with Query: instead of assigning a result to a temporary variable, replace the variable with a query (method call). This makes the intent clearer, enables the extracted method to be reused, and eliminates the risk of temporary variables being set incorrectly.",
        ],
    },

    {
        "title": "The Mythical Man-Month: Essays on Software Engineering",
        "author": "Frederick P. Brooks Jr.",
        "year": 1975,
        "memories": [
            "Brooks' Law: adding manpower to a late software project makes it later. New programmers require training, which takes time from experienced developers. New people add complexity to team communication (n people = n*(n-1)/2 communication channels). Brooks' Law remains one of the most important and most violated insights in software engineering.",
            "The Mythical Man-Month: the fallacy of treating software development as if programming tasks can be parallelized across workers like farm labor. A woman can have a baby in nine months; nine women cannot have a baby in one month. Some tasks are inherently sequential and cannot be decomposed into parallel work.",
            "The conceptual integrity principle: Brooks argues that the most important property of a large software system is conceptual integrity — the system should present a coherent, unified concept to the user. A system built by one architect with a clear vision is better than one built by many with conflicting visions.",
            "No silver bullet: Brooks' 1986 essay arguing there is no single development technique that can improve software development productivity by even one order of magnitude in ten years. The essential difficulties of software (complexity, conformity, changeability, invisibility) are irreducible.",
            "Accidental vs. essential difficulties: accidental difficulties are those arising from the tools, languages, and environments we use — they can be reduced. Essential difficulties are those inherent in the nature of software itself — they cannot be eliminated. Most productivity improvements address accidental difficulties.",
            "The tar pit metaphor: programming is like a prehistoric tar pit — the more you thrash, the deeper you sink. Complex systems are seductive because they look manageable until you're in them. Brooks describes the joys and woes of the craft to explain why programmers persist despite the difficulty.",
            "Plan to throw one away: when building a new system, plan to build it twice — the first version is the one you learn from. The second version benefits from everything you learned building the first. Organizations that expect the first version to be the final version are setting themselves up for architectural failure.",
            "The pilot system: Brooks recommends building a small pilot system first to explore the requirements and architecture before committing to the full production system. Similar to tracer bullets — validate your design assumptions before investing fully.",
            "The surgical team: instead of organizing a large team as equals, Brooks proposes a surgeon/copilot model — one chief programmer designs everything and writes all critical code, with others supporting. This preserves conceptual integrity while still providing support for the most important work.",
            "Communication and team size: the communication overhead of a team scales quadratically with team size. Two people: 1 communication channel. Four people: 6 channels. Eight people: 28 channels. This is why small teams are vastly more productive per person than large teams.",
            "Documentation as design: Brooks argues that writing a specification forces you to make decisions you would otherwise defer. The act of writing reveals inconsistencies and gaps in thinking. A spec is not bureaucracy — it's the first executable specification of the design.",
            "Software system scheduling: Brooks' rule of thumb for scheduling: 1/3 of time for planning, 1/6 for coding, 1/4 for component test and early system test, 1/4 for system test with all components in hand. Most projects allocate far too little time for testing.",
            "The second-system effect: an architect's second system is the most dangerous. The first system is constrained by inexperience. The second system allows the architect to incorporate all the ideas they couldn't fit into the first — leading to over-engineering and feature bloat.",
        ],
    },

    {
        "title": "Working Effectively with Legacy Code",
        "author": "Michael C. Feathers",
        "year": 2004,
        "memories": [
            "Feathers' definition of legacy code: code without tests. This reframes the problem — it's not old code or bad code that makes it legacy, it's the absence of a safety net that would allow you to change it confidently. This definition makes legacy code about a testing deficit, not an age deficit.",
            "The Legacy Code Change Algorithm: identify change points, find test points, break dependencies, write tests, make changes and refactor. This five-step process applies to any legacy code change, regardless of language or age of the code.",
            "The seam concept: a seam is a place where you can alter behavior in a program without editing in that place. Seams are crucial for testing legacy code — they let you substitute test doubles for real dependencies. Types: preprocessing seams, link seams, object seams. Object seams (subclasses and interfaces) are the most useful.",
            "Sensing and separation: when breaking dependencies for tests, you have two goals — sensing (detecting effects of code you can't verify directly) and separation (extracting the code you want to test from its dependencies). Most legacy code testing challenges reduce to one of these.",
            "The sprout method and class: when adding new functionality to a legacy system, don't add it to existing untested methods. Instead, sprout a new method or class, write tests for it in isolation, and call it from the existing code. This isolates your new work from the untested legacy.",
            "The wrap method and class: when you need to add behavior before or after existing functionality, wrap the existing method in a new method that adds the new behavior. The existing method becomes an implementation detail. This is safer than modifying untested code.",
            "The Characterization Test: a test that pins down the current behavior of a piece of code, not what it should do. When working with legacy code, write characterization tests to document what the code does before changing it. This is your safety net.",
            "Breaking dependencies with Extract and Override: when a method creates objects internally (new Something()), it's hard to test without instantiating real objects. Subclass and override the creation method to inject test doubles. A form of the Subclass and Override pattern.",
            "The hidden dependency problem: many legacy methods have dependencies hidden inside them — database calls, file I/O, network calls, time-of-day calls. These make methods hard to test in isolation. Extract the dependency into a replaceable parameter or interface.",
            "Dealing with the global variable problem: global variables create hidden dependencies that make testing impossible. Replace globals with a parameter, use a getter/setter you can override, or introduce a singleton you can replace. Globals are a design problem, not just a style problem.",
            "The Monster Method: a legacy method hundreds or thousands of lines long that does everything. Impossible to test because it does too much. Break it apart by identifying 'skeletonizing' points — places where you can extract a complete, meaningful chunk of behavior into a new method.",
            "Working with C code: Feathers addresses the challenge of testing C — a language without objects or inherent seams. Techniques: link seams (replace functions at link time), preprocessing seams (use #define to replace function calls), function pointer injection (pass functions as parameters for testing).",
            "The fear cycle: without tests, every change to legacy code is risky. Fear of breaking things leads to minimal changes. Minimal changes lead to increased complexity. Increased complexity makes tests harder to add. This fear cycle is how legacy code perpetuates itself. Breaking the cycle requires investing in tests first.",
            "It takes time: Feathers is realistic that making legacy code testable takes significant time. The first test for a legacy class often requires extensive refactoring. Teams must budget time for this work. But the investment compounds — each test makes the next test easier to write.",
        ],
    },

    {
        "title": "Domain-Driven Design: Tackling Complexity in the Heart of Software",
        "author": "Eric Evans",
        "year": 2003,
        "memories": [
            "Domain-Driven Design's central premise: the most significant complexity in large software systems is not technical — it's in the domain itself, the business problem the software solves. To build systems that solve complex problems well, developers must deeply understand and model the domain.",
            "The ubiquitous language: a shared language between developers and domain experts, used consistently in code, in conversation, in documentation, and in tests. Every class name, method name, and variable name should come from the domain language. When the code doesn't use the same words as the business, translation errors creep in.",
            "The model-driven design principle: the code should be a direct expression of the model. Domain experts can look at the code and recognize the concepts they described. When domain experts and developers use the same language to describe the same concepts, the model is unified.",
            "Entities: objects with a thread of identity that persists over time and across different states. An entity is defined by its identity, not its attributes. A person is an entity — they may change their name, address, and hair color, but they remain the same person (same identity).",
            "Value Objects: objects that are defined entirely by their attributes, with no identity. Two value objects with the same attributes are the same. Money (amount + currency), Color, Address are typically value objects. Value objects should be immutable. Prefer value objects over entities when identity doesn't matter.",
            "Aggregates: a cluster of associated objects treated as a unit for data changes. Each aggregate has a root entity (the aggregate root) through which all external access must pass. The aggregate root enforces invariants for the whole cluster. This keeps consistency boundaries explicit.",
            "Domain Services: when an operation doesn't naturally belong to any domain object, put it in a domain service. Domain services are stateless operations that express important business rules or computations. Name them with domain vocabulary. Don't confuse with application services (orchestration) or infrastructure services (persistence).",
            "Repositories: provide access to stored aggregates as if they were in-memory collections. The client code doesn't know whether data comes from a database, a file, or memory. The repository abstracts persistence from the domain model. One repository per aggregate root.",
            "Factories: handle complex object creation logic that doesn't belong in the object itself or in a client. When creating an aggregate is complex (multiple steps, invariants to enforce), a factory hides this complexity. Factories ensure objects are created in a valid state.",
            "Bounded Contexts: the explicit boundary within which a domain model applies. In a large enterprise, different teams build different models for different purposes. An Order in the shopping context has different attributes than an Order in the shipping context. Bounded contexts make these differences explicit.",
            "Context Maps: a visualization of the relationships between bounded contexts in a large system. Shows how models in different contexts relate to each other. Integration patterns between contexts: Shared Kernel, Customer/Supplier, Conformist, Anti-Corruption Layer, Separate Ways, Published Language.",
            "The Anti-Corruption Layer: a translation layer that isolates your domain model from a legacy system or third-party service. Without an ACL, your model gets corrupted by the foreign model's concepts and vocabulary. The ACL translates between the two models.",
            "Strategic design: DDD distinguishes between tactical design (patterns for implementing models) and strategic design (patterns for organizing large systems). Strategic design addresses the big picture — how to divide a large domain into bounded contexts and how those contexts interact.",
            "Supple design: Evans' vision of code that is not just correct but pleasurable to work with. Supple design is deeply expressive, easy to understand, and supports deep exploration. It requires: intention-revealing interfaces, side-effect-free functions, assertions (documented invariants), conceptual contours, and standalone classes.",
            "Continuous integration of the model: the model must be kept unified and consistent throughout the project. DDD requires teams to continuously integrate their understanding of the model — not just the code, but the concepts, language, and insights from domain experts. Models that aren't continuously integrated become fragmented.",
        ],
    },

    {
        "title": "Effective Java",
        "author": "Joshua Bloch",
        "year": 2001,
        "memories": [
            "Item 1 — Consider static factory methods instead of constructors: static factory methods have names (unlike constructors), don't have to return a new object on each invocation (singleton, caching), and can return any subtype. Disadvantage: classes without public/protected constructors can't be subclassed.",
            "Item 15 — Minimize the accessibility of classes and members: make each class or member as inaccessible as possible. Information hiding — a fundamental principle of modular design — decouples components, enables parallel development, eases maintenance, and reduces risk. Never expose mutable state.",
            "Item 17 — Minimize mutability (immutable classes): immutable classes are simpler, safer, and easier to use correctly than mutable classes. They're inherently thread-safe, can be shared freely, and make for better building blocks. The cons (separate object for each distinct value) are manageable for most use cases.",
            "Item 18 — Favor composition over inheritance: inheritance violates encapsulation. A subclass depends on implementation details of its superclass. If the superclass changes, the subclass may break. Composition (where a class has a field of another class type) avoids this fragility.",
            "Item 39 — Prefer annotations to naming patterns: naming conventions like 'testXxx' for test methods are fragile, error-prone, and have no automatic enforcement. Annotations are enforced by the compiler, can carry parameters, and can apply to different program elements. Use annotations instead of naming conventions.",
            "Item 42 — Prefer lambdas to anonymous classes: anonymous classes for function objects are verbose. As of Java 8, lambdas provide a much cleaner syntax. Lambdas are best for small, one-use functions. If a method has a useful name, prefer a method reference. If the lambda is longer than a few lines, use a named method.",
            "Item 46 — Prefer side-effect-free functions in streams: the forEach operation is for reporting results of stream computations, not for performing computations. Computations should use collectors (toList, toSet, groupingBy, counting). Writing code that looks like a stream but isn't is worse than not using streams at all.",
            "Item 55 — Return optionals judiciously: Optional is Java's way of returning an empty result instead of null or throwing an exception. Never return null from a method declared to return Optional. Never store null in an Optional field. Optionals have a performance cost — don't use them for primitive type fields.",
            "Item 57 — Minimize the scope of local variables: declare local variables where they are first used, not at the top of a block. Every variable should be initialized with as narrow a scope as feasible. This prevents accidental misuse and makes the code easier to read.",
            "Item 63 — Beware the performance of string concatenation: the string concatenation operator (+) requires linear time proportional to the number of characters in the strings being concatenated. For large numbers of concatenations, use StringBuilder. Don't use string concatenation in loops.",
            "Item 78 — Synchronize access to shared mutable data: when multiple threads share mutable data, all reads and writes must be synchronized. Without synchronization, a thread may never observe changes made by another thread. The Java Memory Model does not guarantee visibility of changes between threads without synchronization.",
            "Item 80 — Prefer executors, tasks, and streams to threads: working directly with threads is error-prone. Use java.util.concurrent: ExecutorService for task execution, CountDownLatch for synchronization, ConcurrentHashMap for concurrent maps. Parallel streams automatically parallelize work using the common fork-join pool.",
            "Item 89 — Prefer enums to readResolve for instance control: enum types provide compile-time type safety, their constants are final, and their semantics are clearer than integer or string constants. Prefer enum types for any fixed set of constants, including strategy objects.",
            "Design patterns in Effective Java: Bloch's book is implicitly a pattern book — Builder (item 2), Factory Method (item 1), Singleton (item 3), Strategy (item 22, via interfaces), Template Method (item 20, via abstract classes), Flyweight (item 17, via immutables). The patterns are presented in idiomatic Java.",
        ],
    },

    {
        "title": "Grokking Algorithms: An Illustrated Guide for Programmers and Other Curious People",
        "author": "Aditya Bhargava",
        "year": 2016,
        "memories": [
            "Binary search: given a sorted array of n items, binary search takes O(log n) time. Each step eliminates half the remaining items. For 100 items: 7 steps. For 1,000,000 items: 20 steps. Linear search would require up to 1,000,000 steps. Binary search is only applicable to sorted data structures.",
            "Big O notation: describes how the runtime or space requirements of an algorithm grow as input size grows. O(1) — constant time (array access). O(log n) — logarithmic (binary search). O(n) — linear time (simple search). O(n log n) — log-linear (efficient sorting). O(n²) — quadratic (selection sort). O(2^n) — exponential. O(n!) — factorial (traveling salesman brute force).",
            "Selection sort: for each position in the array, find the minimum element in the unsorted portion and swap it into position. Simple but O(n²) — slow for large datasets. Each pass through the array is O(n), and we do n passes. Insertion sort is usually better in practice because it adapts to already-sorted data.",
            "Recursion: a function that calls itself. Every recursive function has two cases: the base case (when to stop) and the recursive case (when to call itself again). The call stack holds the state of each recursive call. Deep recursion can cause stack overflow. Tail call optimization eliminates this for languages that support it.",
            "Hash tables: key-value lookup in O(1) average time. A hash function maps keys to positions in an array. Collisions (two keys mapping to the same position) are resolved by chaining (linked list at each position) or open addressing (find next empty slot). Load factor (items/slots) determines performance.",
            "Breadth-first search (BFS): explores a graph level by level. Uses a queue (FIFO). Finds the shortest path in an unweighted graph. Running time: O(V + E) where V = vertices and E = edges. Use BFS when you want the minimum number of steps to reach a target.",
            "Dijkstra's algorithm: finds the shortest path in a weighted graph with non-negative weights. Unlike BFS (which finds fewest edges), Dijkstra finds the lowest total weight path. Doesn't work with negative-weight edges — use Bellman-Ford for those. Running time: O((V + E) log V) with a priority queue.",
            "Greedy algorithms: make the locally optimal choice at each step, hoping to find a global optimum. The set cover problem example: pick the station that covers the most uncovered states at each step. Greedy algorithms are fast but don't always produce the optimal solution — they produce a 'good enough' solution.",
            "Dynamic programming: solve complex problems by breaking them into overlapping sub-problems and storing the results (memoization or tabulation). Classic examples: the knapsack problem, longest common subsequence, shortest path in a weighted graph. Dynamic programming turns exponential algorithms into polynomial ones.",
            "The knapsack problem: given items with weights and values, and a knapsack with a weight limit, find the maximum value you can carry. The greedy approach doesn't always work. Dynamic programming fills a grid of sub-problems, building up to the solution. A classic example of when greedy fails and DP is necessary.",
            "K-nearest neighbors (KNN): classify a new item by finding the k most similar items in the training data and using their classifications (majority vote). For regression, take the average of the k nearest neighbors' values. KNN is simple but slow for large datasets — O(nd) where n = training examples and d = dimensions.",
            "The traveling salesman problem (TSP): find the shortest route visiting every city exactly once. O(n!) brute force — 10 cities = 3.6 million routes, 30 cities = 2.65 × 10^32 routes. TSP is NP-hard — no known polynomial-time solution. Real solutions use approximation algorithms and heuristics.",
        ],
    },

    {
        "title": "Programming Pearls",
        "author": "Jon Bentley",
        "year": 1986,
        "memories": [
            "Programming Pearls' central theme: the right approach to a programming problem is often non-obvious, and the most elegant solutions typically involve insight rather than brute force. Bentley teaches problem-solving by example — real problems solved in better-than-expected ways.",
            "The bit vector sort: given up to 10 million distinct telephone numbers, sort them. Naive approach: read all numbers into an array, sort with O(n log n) sort. Bentley's insight: use a bit vector. One bit per possible number (10 million bits = 1.25 MB). Set bit i if number i is present. Then iterate over all bits — the numbers appear in sorted order. O(n) with very low memory.",
            "Binary search rediscovered: most binary search implementations have bugs — incorrect loop termination, overflow in the midpoint calculation ((lo + hi) / 2 overflows when lo and hi are large ints). The correct calculation: lo + (hi - lo) / 2. Bentley's lesson: simple programs are harder to write correctly than they appear.",
            "Back-of-envelope calculations: Bentley teaches estimation as a core programming skill. Numbers every programmer should know: single disk seek (~10ms), sequential disk read (~50MB/s), network packet round trip (~100ms). These numbers let you estimate whether an approach will work before implementing it.",
            "Little's Law: in any stable system, the average number of items in the system equals the average arrival rate times the average time each item spends in the system (L = λW). Applied to software: if your web server processes 100 requests/second and each request takes 2 seconds, you have 200 requests in flight at any time.",
            "Profile before optimizing: Bentley's empirical studies found that in most programs, 90% of the execution time is spent in 10% of the code. Optimizing the wrong 90% has no effect. Profile first to find the actual hot spots. Then optimize only those.",
            "Space-time tradeoffs: the eternal software optimization trade-off. Use more space (precompute and cache results, maintain auxiliary data structures) to save time. Or use less space (recompute values, stream data) to save memory. The right choice depends on whether the system is memory-bound or CPU-bound.",
            "Algorithm selection matters more than micro-optimization: switching from an O(n²) algorithm to an O(n log n) algorithm for large inputs provides far more improvement than any constant-factor optimization. An O(n²) algorithm with n=1,000,000 requires 10^12 operations; O(n log n) requires only 20 million.",
            "The column approach to problem-solving: Bentley describes 'column' thinking — problems presented in programmer magazines with 'clean' solutions that look obvious in retrospect but required significant insight to find. The lesson: any problem has a simpler solution than the first approach. Keep looking.",
            "Aho-Corasick and string matching: building efficient string matching algorithms. Naive: O(n*m) where n = text length and m = pattern length. KMP and Boyer-Moore achieve O(n+m). Bentley shows how understanding the structure of the problem leads to fundamentally better algorithms.",
            "Heaps and priority queues: the heap data structure allows O(log n) insertion and O(log n) extraction of the minimum (or maximum). Heaps underlie priority queues, Dijkstra's algorithm, heap sort, and scheduling algorithms. Understanding the heap structure (complete binary tree stored in an array) is foundational.",
        ],
    },

    {
        "title": "Build a Large Language Model (From Scratch)",
        "author": "Sebastian Raschka",
        "year": 2024,
        "memories": [
            "Building an LLM from scratch: Raschka's book walks through implementing a GPT-style large language model in Python with PyTorch, from tokenization through pre-training, fine-tuning for instruction following, and RLHF. The goal is understanding, not production deployment.",
            "Tokenization: LLMs don't process characters or words — they process tokens. The byte pair encoding (BPE) algorithm starts with individual bytes/characters and iteratively merges the most frequent pairs into new tokens. GPT-2 uses 50,257 tokens. Tokenization affects both model performance and computational cost.",
            "The attention mechanism: the core of transformer models. Self-attention computes a weighted sum of input representations, where weights come from compatibility scores between query and key vectors. Multi-head attention runs several attention operations in parallel, each learning different aspects of relationships between tokens.",
            "The transformer architecture: input tokens → token embeddings + positional embeddings → N transformer blocks (each: multi-head attention + feed-forward network + layer norm + residual connections) → linear output layer + softmax → probability distribution over vocabulary.",
            "Training a GPT model: pre-training uses next-token prediction on massive text corpora. Given tokens [t1, t2, t3], predict t4. The loss is cross-entropy between the predicted probability distribution and the one-hot encoding of the actual next token. Minimizing this loss over trillions of tokens produces powerful representations.",
            "Instruction fine-tuning: a pre-trained LLM outputs coherent text but doesn't follow instructions. Fine-tuning on instruction-response pairs (supervised fine-tuning, SFT) teaches the model to respond helpfully to prompts. The Alpaca and FLAN datasets are examples of instruction fine-tuning datasets.",
            "RLHF (Reinforcement Learning from Human Feedback): after SFT, train a reward model to predict human preferences between responses. Use proximal policy optimization (PPO) to fine-tune the language model to maximize the reward model's scores. This alignment technique is what makes models like ChatGPT responsive and helpful.",
            "Positional encodings: attention is permutation-invariant — it doesn't inherently know the order of tokens. Positional encodings inject order information. Fixed sinusoidal encodings (original transformer) or learned positional embeddings (GPT) both work. Rotary Position Embedding (RoPE, used in Llama) scales better to long contexts.",
            "Context length and KV cache: the context window defines how many tokens the model can 'see' at once. During inference, the key-value pairs from the attention computation can be cached (KV cache) to avoid recomputing them for each new token generated. This is why inference is faster than training per-token.",
            "Model scaling: larger models trained on more data generally perform better. Chinchilla scaling laws (DeepMind, 2022): compute-optimal training trains a model with N parameters on approximately 20*N tokens. GPT-3 (175B params) was undertrained; models like Llama 3 train on 15+ trillion tokens.",
            "Quantization: reducing the precision of model weights from float32 (4 bytes/param) to float16 (2 bytes), bfloat16 (2 bytes), or int8/int4 (1/0.5 bytes). Reduces memory requirements and speeds up inference with minimal accuracy loss. A 7B parameter model in float16 requires 14GB; in int4, only 3.5GB.",
            "The emergent capabilities phenomenon: as LLMs scale, they exhibit capabilities not seen in smaller models — few-shot learning, chain-of-thought reasoning, code generation, multi-step problem solving. These capabilities emerge at specific scale thresholds and are not explicitly trained for. Understanding why remains an active research question.",
        ],
    },
]


def main():
    global _current_book
    log("="*60)
    log(f"Programming Classics Ingest")
    log(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    total_planned = sum(len(b["memories"]) for b in BOOKS)
    log(f"Books: {len(BOOKS)} · Memories: {total_planned}")
    log("="*60)

    post_notify(
        f":books: *Programming Classics — Ingest Starting*\n"
        f"• 13 books: Pragmatic Programmer, Clean Code, GoF Design Patterns, Code Complete, "
        f"Refactoring, Mythical Man-Month, Working with Legacy Code, DDD, Effective Java, "
        f"Grokking Algorithms, Programming Pearls, Build an LLM\n"
        f"• Memories to store: *{total_planned}*\n"
        f"• Source: `{SOURCE}`\n"
        f"• Progress updates every 5 minutes"
    )

    for book in BOOKS:
        ingest_book(book["title"], book["author"], book["year"], book["memories"])

    elapsed = int(time.time() - _start_time)
    m, s = divmod(elapsed, 60)
    log(f"\nComplete! {_total_memories} memories stored in {m}m {s}s")

    post_notify(
        f":white_check_mark: *Programming Classics — Ingest Complete*\n"
        f"• Memories stored: *{_total_memories}*\n"
        f"• Source: `{SOURCE}`\n"
        f"• Time: {m}m {s}s"
    )


if __name__ == "__main__":
    main()
