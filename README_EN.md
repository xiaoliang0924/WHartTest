# WHartTest - AI-Powered Intelligent Test Case Generation Platform

English | [中文](README.md)

## Overview

WHartTest is an AI-powered intelligent test management platform for QA teams. It covers requirements, test cases, API and UI automation, task execution, and reporting, with knowledge-base and LLM assistance for authoring, review, and execution.

## Core Features

### 1. AI-Powered Test Case Generation
- Automatically generate test cases from requirement documents, business descriptions, and knowledge base context.
- Support multi-step reasoning with context completion, test case optimization, and result tracking.
- Support Prompt templates, test case templates, and generation strategy configuration to help teams standardize testing practices.

### 2. Requirement Management and Intelligent Review
- Support requirement document upload, parsing, viewing, and online editing, covering the full workflow from requirement import to review reports.
- Support requirement splitting, specialized analysis, context checks, and versioned report viewing.
- Combine requirement content with knowledge bases and LLM capabilities to identify missing information, ambiguity, and key testing concerns.

### 3. Test Case Management
- Manage functional test cases by project and module, including case name, priority, type, preconditions, steps, expected results, and notes.
- Provide both list view and mind map view, supporting quick module/case creation, editing, copy/paste, drag-and-drop organization, import, and export.
- Support fully automated AI writing of test cases: generate complete case structures and test steps from requirement documents, business context, knowledge bases, and existing cases.
- Support fully automated AI editing, optimization, and repair of test case content, including completing preconditions, rewriting steps, improving expected results, fixing non-standard cases, and increasing coverage.
- Support fully automated AI execution of test cases with execution process recording; automatically analyze failure causes from logs, screenshots, and reports, and generate repair suggestions.
- Support test suite management, case selection, batch organization, and execution history tracking.

### 4. API Automation Testing
- Manage API testing assets such as API modules, interface definitions, environment variables, global headers, database configurations, functions, and tags.
- Support API test case orchestration, test task creation, task execution, result details, and test report viewing.
- Support fully automated AI writing of API test cases: automatically generate test steps, assertions, variable extraction, and pre/post scripts from API definitions, request parameters, response structures, and business context.
- Support fully automated AI editing and repair of API testing configurations, including request parameters, headers, environment variables, database checks, function scripts, assertion rules, and dependent variables.
- Support API debugging, case execution, and batch task execution; AI can automatically analyze failed results, execution logs, and response content to locate issues and generate repair plans.
- Support API synchronization, environment switching, and multi-project isolation for unified API automation management.

### 5. UI Automation Testing
- Built-in UI automation executor capabilities, including executor management, environment configuration, page objects, page steps, public data, element locators, and case step management.
- Support fully automated AI writing of UI automation cases and scripts: automatically generate page operation steps, element locators, assertions, and test data from pages, elements, business flows, and test objectives.
- Support fully automated AI editing and repair of UI automation steps, analyzing and generating repair suggestions for issues such as invalid locators, unstable waits, abnormal page states, and assertion failures.
- Support fully automated AI execution of UI cases, debug runs, and batch execution; collect logs, screenshots, videos, and Trace data for playback and troubleshooting.
- Integrate with test cases and the task center to provide unified scheduling and tracking for UI automation tasks.

### 6. Agent Chat and Tool Calls
- Provide an intelligent chat entry point for test analysis, test case generation, and tool calls within project context.
- Support external tool integration and configuration to extend testing, analysis, and automation.
- Support tool approval, system prompts, token usage display, and multi-model configuration to improve controllability and security of AI calls.

### 7. Knowledge Base and Retrieval Augmentation
- Support knowledge base creation, document upload, document chunking, vectorization, retrieval queries, and statistics.
- Support embedding models, Reranker, global knowledge base configuration, and connection testing.
- Turn product documents, API documents, and business rules into project knowledge, providing RAG context for AI generation and review.

### 8. Skills Library
- Support Skills installation, management, and skill store source configuration to extend Agent capabilities.
- Support automation skills, enabling AI to work closer to real testing scenarios.
- Skills have high system privileges and are strongly recommended to be used only in intranet or trusted environments.

### 9. Task Center and Execution Tracking
- Provide a unified task center for creating, executing, and viewing automation tasks.
- Support task log viewing, UI test case selection, execution status tracking, and failure troubleshooting.
- Combine functional cases, API testing, and UI automation into a unified test execution entry point.

### 10. Project, Team, and System Management
- Support project management, user management, organization management, permission management, and API Key management.
- Support LLM configuration management, multi-model integration, version update prompts, dark/light themes, and Chinese/English switching.
- Support operation log recording and cleanup configuration for auditing key platform operations.

## Quick Start

### Docker Deployment (Recommended - out of the box)

```bash
# 1. Clone the repo
git clone https://github.com/MGdaasLab/WHartTest.git
cd WHartTest

# 2. Prepare config (use defaults with auto-generated API Key)
cp .env.example .env

# 3. One-command start (choose one of the two)
# Option A: use the deployment script (recommended, auto-selects registry mirrors)
./run_compose.sh

# Option B: use docker-compose directly
docker-compose up -d

# 4. Open the system
# http://localhost:8913 (admin/admin123456)
```

**That's it!**

### Unified Deployment Script

If you use the built-in deployment script, it now asks you to choose between **remote image pull** and **local image build** at startup:

```bash
./run_compose.sh
```



> ⚠️ **Production note**: Log in to the admin panel, delete the default API Key, and create a new secure key.


## Common Environment Variables

After copying `.env.example` to `.env`, you can adjust file-management settings as needed:

```env
# File management: single-file upload size limit in bytes, default 100MB
FILE_MANAGEMENT_MAX_FILE_SIZE=104857600

# LLM attachments: per-file parsed text character limit; 0 means unlimited. Over-limit files fail with an error and are not truncated.
FILE_MANAGEMENT_LLM_MAX_CHARS_PER_FILE=0
```

**WHartTest** - AI-powered test case generation that makes testing smarter and development more efficient!
