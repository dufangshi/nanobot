# Supervisor Workflow Architecture

## Status

Draft design document for the `nanobot` coding-agent orchestration system.

This document captures the current agreed direction so the design can evolve in-repo instead of staying in chat history.

## Goals

Build a workflow-driven orchestration layer that allows `nanobot` to:

- accept a high-level user task through the main `nanobot` conversation thread
- choose or generate a workflow for that task
- execute coding work through external coding agents such as Codex or Claude Code
- keep `nanobot` responsive while external work runs in the background
- maintain structured task state, plans, gates, reviews, and reports
- support human-in-the-loop updates that may revise the workflow during execution

## Non-Goals

- making the main `nanobot` agent directly execute all coding work itself
- streaming every raw worker log line directly to the user
- requiring a single long-lived LLM session to remember the whole workflow
- binding execution to one backend such as Codex only

## Core Roles

### User

The user is the external stakeholder who assigns a high-level task and may later:

- confirm or reject plans
- add new requirements
- change priorities
- ask for status

### CEO

The CEO is the main `nanobot` conversational role facing the user.

Responsibilities:

- accept user requests
- translate user intent into structured task requests
- decide when to call planning, workflow-generation, or reporting capabilities
- send control messages to the Supervisor
- present user-facing updates in clean language

The CEO should not directly supervise raw worker logs or perform code review itself.

### Supervisor

The Supervisor is the workflow orchestrator.

Responsibilities:

- own task state
- own workflow state
- update multi-step plans
- dispatch worker runs
- dispatch reviewer runs
- react to watcher events
- determine gates, retries, replans, and escalation
- produce structured alerts for the CEO
- write durable task artifacts to disk

The Supervisor should be implemented primarily as a deterministic runtime, not as a single always-on LLM conversation.

### Worker

A Worker is an external coding agent run used to perform a concrete task step.

Examples:

- Codex
- Claude Code

### Reviewer

A Reviewer is an external coding agent run used to validate Worker output.

Examples:

- code review against acceptance criteria
- test verification
- implementation completeness verification

### Watcher

A Watcher is a runtime component owned by the Supervisor.

Responsibilities:

- poll or subscribe to external run events
- normalize backend-specific events
- append run event logs
- update run and step status
- emit structured internal signals to the Supervisor

The Watcher does not talk to the user directly.

## Architecture Summary

High-level flow:

1. User sends a task to the CEO.
2. CEO creates or updates a structured task request.
3. Supervisor creates a task record and selects or generates a workflow.
4. Supervisor launches planner, worker, or reviewer runs through the orchestration MCP.
5. Watchers observe external runs and feed structured events back into Supervisor state.
6. Supervisor updates task progress, gates, review outcomes, and reports.
7. CEO reports to the user only when policy requires it or when the user asks.

## Why the Supervisor Should Not Be a Pure LLM Agent

The Supervisor needs durable control over:

- state transitions
- retries
- cancellations
- approvals
- run tracking
- report persistence
- re-planning triggers

Those concerns are better handled by deterministic code.

LLM-backed subagents are still useful, but they should be invoked as specialized nodes:

- planner
- workflow generator
- worker
- reviewer
- replanner

## Workflow Strategy

### Guiding Principle

Use a fixed Supervisor control loop and keep the mutable workflow as data.

This means:

- the Supervisor runtime has a stable set of phases
- the task-specific workflow lives in structured task state
- human feedback modifies workflow data, not the runtime itself

### Example Supervisor Phases

- `intake`
- `workflow_generation`
- `plan_review`
- `waiting_for_user`
- `dispatch_step`
- `watch_step`
- `review_step`
- `advance_or_replan`
- `report_ready`
- `done`
- `blocked`
- `failed`

### Workflow Data Model

Each task-specific workflow should contain:

- workflow metadata
- step definitions
- dependencies
- assigned backend and role
- acceptance criteria
- approval requirements
- retry policy
- replan policy
- reporting policy

## Natural-Language Workflow Generation

The system should support natural-language workflow generation.

Target behavior:

1. User describes a task in natural language.
2. CEO decides to generate a workflow.
3. CEO may:
   - generate directly
   - call a workflow-generation skill
   - spawn a planning subagent that uses a workflow-generation skill
4. The generated workflow is converted into a canonical workflow format.
5. Supervisor validates and executes that canonical workflow.

### Important Constraint

The generated workflow should not be executed as free-form text.

Instead:

- allow a human-readable workflow authoring format
- compile or normalize it into a canonical machine-readable workflow schema
- execute only the canonical schema

## OpenClaw-Inspired Direction

### Lobster

What to borrow:

- deterministic runtime mindset
- structured steps and gates
- approvals and resume points
- auditable execution

What not to copy blindly:

- its exact CLI/runtime architecture
- its TypeScript implementation

### OpenProse

What to borrow:

- markdown-friendly authoring style
- LLM-friendly structure for generated workflows
- readability for humans reviewing generated workflows

What not to copy blindly:

- tight coupling to OpenClaw session primitives
- agent-runtime assumptions not present in `nanobot`

### Resulting Design Choice

For `nanobot`, the likely best approach is:

- execution model inspired by Lobster
- authoring style inspired by OpenProse
- Python-native runtime for the Supervisor

## Burr Evaluation

`Burr` looks promising as a foundation for the Supervisor runtime.

Capabilities that match our needs:

- explicit actions and transitions
- async execution
- state persistence and resume
- stepwise execution
- hooks for logging/tracking
- human-in-the-loop pauses
- parallel sub-application support

Recommended use of Burr:

- use Burr as the fixed Supervisor control loop
- store the mutable workflow inside task state
- do not rely on dynamically rebuilding the Burr graph for every user change

If Burr is adopted, the system shape should be:

- Burr handles Supervisor phase transitions
- workflow schema lives in state
- external coding agents remain external runs launched through MCP

## Orchestration MCP

The orchestration MCP is the integration boundary between the Supervisor runtime and external coding agents.

### Core Requirements

- launching a new run must return immediately
- runs must not block the `nanobot` main conversation
- backends must be selectable per run
- sessions must support both new and resume modes
- run progress must be available incrementally

### Candidate Tool Surface

- `spawn_run`
- `get_run`
- `poll_events`
- `cancel_run`
- `list_runs`

### `spawn_run`

Purpose:

- start a planner, worker, or reviewer run
- choose backend
- choose new or resumed backend session

Example fields:

- `backend`
- `role`
- `prompt`
- `cwd`
- `session_mode`
- `session_id`
- `model`
- `profile`
- `output_schema`

### `poll_events`

Preferred behavior:

- long-poll for incremental events
- return once new events exist or timeout expires
- support ordered event retrieval via sequence numbers

This is preferred over:

- blocking one long MCP tool call until task completion
- polling local files as the primary sync mechanism
- unconditional time-based status reports

## Event and Watcher Model

### Why Watchers Still Matter

Even with a strong Supervisor/CEO split, Watchers are still required.

They should be implemented as background async tasks that:

- poll the orchestration MCP
- convert raw backend events into normalized internal events
- update step and run state
- trigger Supervisor signals

### Watcher Output

Watcher output should be internal signals, not direct user messages.

Examples:

- `run_started`
- `run_progressed`
- `run_stalled`
- `run_completed`
- `run_failed`
- `review_required`
- `user_approval_required`

### Normalized Event Types

Candidate normalized run events:

- `run_started`
- `status_changed`
- `agent_message`
- `reasoning`
- `command_started`
- `command_updated`
- `command_finished`
- `file_changed`
- `tool_started`
- `tool_finished`
- `todo_updated`
- `run_completed`
- `run_failed`

## Reporting Strategy

The user should not receive every low-level worker update.

Recommended policy:

- CEO reports when a plan is ready
- CEO reports when user approval is needed
- CEO reports when a major step completes
- CEO reports when a review fails or a task is blocked
- CEO reports when the user explicitly asks for status
- CEO reports final completion

This keeps the system in a "supervisor/foreman" mode instead of a raw log relay mode.

## Example Task: Online Store

User request:

`Write a front-end/back-end separated online store`

Possible generated workflow outline:

1. Research candidate tech stacks and tradeoffs.
2. Produce an architecture proposal.
3. Ask the user to confirm the selected stack.
4. Design the database schema.
5. Review and gate the schema.
6. Implement back-end foundations.
7. Review and gate the back-end foundations.
8. Implement front-end foundations.
9. Review and gate the front-end foundations.
10. Implement store features iteratively.
11. Run tests and review each milestone.
12. Produce a delivery report.

Possible human-in-the-loop modifications:

- change stack selection
- add mobile-first support
- prioritize admin panel later
- switch database choice
- tighten payment-related acceptance criteria

These changes should update the workflow data and possibly trigger replanning.

## Canonical Workflow Direction

We should introduce a canonical workflow schema owned by this project.

Recommended properties:

- backend-agnostic
- serializable
- versioned
- easy to validate
- easy for LLMs to generate
- easy for humans to review

Likely representation:

- JSON for machine execution
- Markdown or YAML-like authoring for human review

## Proposed Persistent Task Layout

Suggested task storage root:

`./.nanobot-supervisor/tasks/<task_id>/`

Suggested files:

- `task.json`
- `workflow.json`
- `workflow.md`
- `report.md`
- `runs/<run_id>.json`
- `runs/<run_id>.events.jsonl`
- `reviews/<review_run_id>.json`

## Proposed Code Layout

Initial proposal:

```text
nanobot/
  supervisor/
    __init__.py
    manager.py
    state.py
    workflow.py
    events.py
    watcher.py
    reporting.py
    storage.py
    adapters/
      __init__.py
      base.py
      codex.py
      claude_code.py
  agent/
    ...
```

Notes:

- `manager.py` owns the Supervisor runtime entry points
- `state.py` defines task, step, run, and alert models
- `workflow.py` defines the canonical workflow schema and validation helpers
- `events.py` defines normalized watcher/supervisor events
- `watcher.py` implements background run monitoring
- `reporting.py` converts Supervisor alerts into CEO-facing summaries
- `storage.py` manages task persistence
- `adapters/` map backend-specific runs to normalized orchestration behavior

## Near-Term Implementation Plan

### Phase 1

- write this architecture document
- define canonical task and workflow models
- define orchestration MCP interface
- define normalized event schema

### Phase 2

- implement Supervisor storage
- implement run adapters
- implement watcher infrastructure
- integrate non-blocking background monitoring

### Phase 3

- implement workflow generation flow
- implement planner and reviewer roles
- implement reporting policy
- support user interrupts and replanning

### Phase 4

- evaluate Burr as runtime foundation
- decide whether to adopt Burr or keep a custom Python state machine
- add workflow templates and workflow-generation skills

## Open Questions

- whether to use Burr or a custom Supervisor runtime first
- whether workflow authoring should be markdown-first, YAML-first, or both
- how strict the canonical workflow schema should be in v1
- how CEO-to-Supervisor control messages should be represented
- how much review automation should happen before involving the user
- whether user approval steps should pause only the affected task or all related runs

## Current Recommendation

Short-term recommendation:

- keep the Supervisor deterministic
- keep workflow mutable as data
- keep CEO as the user-facing layer only
- use watchers as internal sensors
- use external coding agents for planning, execution, and review
- design a canonical workflow schema before committing to a runtime framework
