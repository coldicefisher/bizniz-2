# Bizniz Autocoder System Architecture

_Last updated: 2026-03-08 05:31 UTC_

## Overview

The **Bizniz Autocoder System** is an autonomous code generation and validation framework designed to:
- Generate code from natural language requirements
- Validate functionality through automated testing
- Iteratively repair and refine generated code
- Architect and plan complex systems through AI-driven agents

The system is built around a core abstraction called **BaseAIAgent**, which provides the foundation for specialized agents such as the Autocoder, Autotester, AutoEngineer, and AutoArchitect.

A **CodingOrchestrator** coordinates these agents, managing the lifecycle and loopback between generation, testing, and repair phases.

---

# High-Level Architecture

```
                ┌────────────────────┐
                │    AutoArchitect   │
                │ System Design &    │
                │ Requirements       │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │   AutoEngineer     │
                │ Implementation     │
                │ Planning           │
                └─────────┬──────────┘
                          │
                          ▼
                 ┌───────────────────┐
                 │ CodingOrchestrator│
                 │ Controls Loop     │
                 └───────┬───────────┘
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
  ┌────────────┐  ┌────────────┐  ┌────────────┐
  │  Autocoder │  │ Autotester │  │ Environment│
  │ Code Gen   │  │ Validation │  │ Execution  │
  └────────────┘  └────────────┘  └────────────┘
```

---

# Core Components

## BaseAIAgent

The **BaseAIAgent** class is the foundational abstraction for all AI-driven components.

It standardizes:

- Interaction with the AI client (ChatGPT or others)
- Execution environments (Python sandbox, Docker, etc)
- Workspace management (saving/loading generated code)
- Event callbacks
- Message normalization and structured prompting

### Responsibilities

- AI prompt orchestration
- Tool execution through environments
- Structured response handling
- Retry and error management
- Workspace integration

### Dependencies

| Component | Purpose |
|----------|--------|
| AI Client | Communicates with the LLM |
| Execution Environment | Runs generated code |
| Workspace | Saves code artifacts |
| Event Callbacks | Observability and logging |

---

# Specialized AI Agents

## Autocoder

The **Autocoder** is responsible for generating code that satisfies a provided problem statement.

### Responsibilities

- Convert problem statements into executable code
- Wrap output inside a standardized `process()` entrypoint
- Produce structured JSON output containing:
  - generated code
  - analysis
  - fix plan (if repairing)

### Workflow

1. Receive input data and problem prompt
2. Generate code
3. Execute the code in the environment
4. Return results to the orchestrator

### Key Goals

- Deterministic code output
- Structured responses
- Repair capability

---

## Autotester

The **Autotester** validates generated code against requirements.

### Responsibilities

- Execute the generated code
- Validate output using a **Validator**
- Provide structured error information
- Produce debugging information for repair attempts

### Validation Output

The tester returns a **ValidationResult** containing:

- `is_valid`
- `errors`
- `expected_output`
- `actual_output`

This information feeds back into the repair loop.

---

## AutoEngineer

The **AutoEngineer** acts as the **implementation planner**.

### Responsibilities

- Break system requirements into implementation tasks
- Define module boundaries
- Specify function contracts
- Generate step-by-step coding plans

### Example Output

- Module architecture
- Function definitions
- Interface contracts
- Implementation sequence

The output of the engineer becomes input for the **Autocoder**.

---

## AutoArchitect

The **AutoArchitect** is the highest-level planning agent.

### Responsibilities

- Interpret high-level system requirements
- Design system architecture
- Define modules and service boundaries
- Produce engineering plans

### Outputs

- System architecture documents
- Component breakdowns
- Engineering plans for AutoEngineer

---

# CodingOrchestrator

The **CodingOrchestrator** is responsible for coordinating the entire system.

It controls:

- agent sequencing
- retry logic
- repair loops
- result aggregation

### Core Responsibilities

- Manage code generation attempts
- Invoke testing
- Trigger repair cycles
- Track attempt history
- Manage stopping conditions

---

# Code Generation Loop

The core system loop works as follows:

```
Problem Statement
        │
        ▼
AutoEngineer (implementation plan)
        │
        ▼
Autocoder (generate code)
        │
        ▼
Execution Environment
        │
        ▼
Autotester (validate output)
        │
        ├── PASS → Return result
        │
        └── FAIL → Repair Loop
                        │
                        ▼
                  Autocoder Repair
                        │
                        ▼
                    Retest
```

---

# Execution Environments

The system supports multiple execution environments:

## Python Sandbox Environment

- restricted execution
- limited imports
- safe builtins

Used primarily for testing.

## Docker Execution Environment

- full Python runtime
- resource limits
- process isolation

Used for production execution.

---

# Workspace

The **Workspace** stores artifacts generated during execution.

Artifacts may include:

- generated code
- test results
- repair attempts
- debugging information

Example structure:

```
workspace/
    code/
        attempt_1.py
        attempt_2.py
    logs/
    outputs/
```

---

# Error Handling Model

Execution failures are normalized into a structured format:

```
ExecutionEnvironmentErrorDetails
```

Fields include:

- stage
- error type
- message
- line number
- code line
- traceback
- stdout
- stderr

This structure enables reliable debugging feedback to the AI agents.

---

# Design Goals

The system was designed around the following principles:

### Determinism

Structured outputs reduce hallucination risk.

### Observability

Events, artifacts, and errors are persisted.

### Repairability

Every failure feeds back into a repair loop.

### Environment Isolation

Code execution occurs inside controlled environments.

### Agent Specialization

Each AI agent has a clearly defined role.

---

# Future Extensions

Potential extensions include:

- distributed execution environments
- multi-language support
- automated dependency resolution
- multi-agent debate systems
- performance benchmarking
- cost-aware code generation

---

# Summary

The **Bizniz Autocoder System** combines AI agents, execution environments, and orchestrators to form a fully automated code development pipeline.

By separating responsibilities between architecture, planning, generation, testing, and orchestration, the system achieves a modular and extensible design suitable for autonomous software engineering workflows.
