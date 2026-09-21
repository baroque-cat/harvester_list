# Harvester - Universal Data Acquisition Framework

**📖 [中文文档](README.zh-CN.md) | English | 🔗 [More Tools](https://github.com/wzdnzd/ai-collector)**

A universal, adaptive data acquisition framework designed for comprehensive information acquisition from multiple sources including GitHub, network mapping platforms (FOFA, Shodan), and arbitrary web endpoints. While the current implementation focuses on AI service provider key discovery as a practical example, the framework is architected for extensibility to support diverse data acquisition scenarios.

---

⭐⭐⭐ **If this project helps you, please give it a star!** Your support motivates us to keep improving and adding new features.

---

## Table of Contents

- [Key Features](#key-features)
- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Directory Structure](#directory-structure)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)

## Project Goals

The system aims to build a **universal data acquisition framework** primarily targeting:

- **GitHub**: Code repositories, issues, commits, and API endpoints
- **Network Mapping Platforms**: 
  - [FOFA](https://fofa.info) - Cyberspace mapping and asset discovery
  - [Shodan](https://www.shodan.io/) - Internet-connected device search engine
- **Arbitrary Web Endpoints**: Custom APIs, web services, and data sources
- **Extensible Architecture**: Plugin-based system for easy integration of new data sources

## Current Data Source Support

| Data Source | Status        | Description                             |
| ----------- | ------------- | --------------------------------------- |
| GitHub API  | ✅ Implemented | Full API integration with rate limiting |
| GitHub Web  | ✅ Implemented | Web scraping with intelligent parsing   |
| FOFA        | 🚧 Planned     | Cyberspace asset discovery integration  |
| Shodan      | 🚧 Planned     | IoT and network device enumeration      |
| Custom APIs | 🚧 Planned     | Generic REST/GraphQL API adapter        |

## Architecture

### Layered Architecture

```mermaid
graph TB
    %% Entry Layer
    subgraph Entry["Entry Layer"]
        CLI["CLI Interface<br/>(main.py)"]
        App["Application Core<br/>(main.py)"]
    end

    %% Management Layer
    subgraph Management["Management Layer"]
        TaskMgr["Task Manager<br/>(manager/task.py)"]
        Pipeline["Pipeline Manager<br/>(manager/pipeline.py)"]
        WorkerMgr["Worker Manager<br/>(manager/worker.py)"]
        QueueMgr["Queue Manager<br/>(manager/queue.py)"]
        StatusMgr["Status Manager<br/>(manager/status.py)"]
        Shutdown["Shutdown Coordinator<br/>(manager/shutdown.py)"]
    end

    %% Processing Layer
    subgraph Processing["Processing Layer"]
        StageBase["Stage Framework<br/>(stage/base.py)"]
        StageImpl["Stage Implementations<br/>(stage/definition.py)"]
        StageReg["Stage Registry<br/>(stage/registry.py)"]
        StageFactory["Stage Factory<br/>(stage/factory.py)"]
        StageResolver["Dependency Resolver<br/>(stage/resolver.py)"]
    end

    %% Service Layer
    subgraph Service["Service Layer"]
        SearchSvc["Search Service<br/>(search/client.py)"]
        SearchProviders["Search Providers<br/>(search/provider/)"]
        RefineSvc["Query Refinement<br/>(refine/)"]
        RefineEngine["Refine Engine<br/>(refine/engine.py)"]
        RefineOptimizer["Query Optimizer<br/>(refine/optimizer.py)"]
    end

    %% Core Domain Layer
    subgraph Core["Core Domain Layer"]
        Models["Domain Models & Tasks<br/>(core/models.py)"]
        Types["Type System<br/>(core/types.py)"]
        Enums["Enumerations<br/>(core/enums.py)"]
        Metrics["Metrics<br/>(core/metrics.py)"]
        Auth["Authentication<br/>(core/auth.py)"]
    end

    %% Infrastructure Layer
    subgraph Infrastructure["Infrastructure Layer"]
        Config["Configuration<br/>(config/)"]
        Tools["Tools & Utilities<br/>(tools/)"]
        Constants["Constants<br/>(constant/)"]
        Storage["Storage & Persistence<br/>(storage/)"]
    end

    %% State Management Layer
    subgraph StateLayer["State Management Layer"]
        StateCollector["State Collector<br/>(state/collector.py)"]
        StateDisplay["Display Engine<br/>(state/display.py)"]
        StateBuilder["Status Builder<br/>(state/builder.py)"]
        StateModels["State Models<br/>(state/models.py)"]
        StateMonitor["State Monitor<br/>(state/monitor.py)"]
        StateEnums["State Enums<br/>(state/enums.py)"]
        StateTypes["State Types<br/>(state/types.py)"]
    end

    %% External Systems
    subgraph External["External Systems"]
        GitHub["GitHub<br/>(API + Web)"]
        AIServices["AI Service<br/>Providers"]
        FileSystem["File System<br/>(Local Storage)"]
    end

    %% Dependencies (Top-down)
    Entry --> Management
    Management --> Processing
    Processing --> Service
    Service --> Core

    %% Infrastructure dependencies
    Entry -.-> Infrastructure
    Management -.-> Infrastructure
    Processing -.-> Infrastructure
    Service -.-> Infrastructure
    Core -.-> Infrastructure

    %% State management dependencies
    Entry -.-> StateLayer
    Management -.-> StateLayer

    %% External dependencies
    Service --> External
    Infrastructure --> External
```

### System Architecture Overview

```mermaid
graph TB
    %% User Interface Layer
    subgraph UserLayer["User Interface Layer"]
        User[User]
        CLI[Command Line Interface]
        ConfigMgmt[Configuration Management]
    end

    %% Application Management Layer
    subgraph AppLayer["Application Management Layer"]
        MainApp[Main Application]
        TaskManager[Task Manager]
        StatusManager[Status Manager]
        ResourceManager[Resource Manager]
        ShutdownManager[Shutdown Manager]
    end

    %% Core Pipeline Engine
    subgraph PipelineCore["Pipeline Engine"]
        %% Stage Management System
        subgraph StageSystem["Stage Management System"]
            StageRegistry[Stage Registry]
            DependencyResolver[Dependency Resolver]
            StageFactory[Stage Factory]
        end

        %% Queue Management System
        subgraph QueueSystem["Queue Management System"]
            QueueManager[Queue Manager]
            WorkerManager[Worker Manager]
            MonitoringSystem[System Monitor]
        end

        %% Processing Stages
        subgraph ProcessingStages["Processing Stages"]
            SearchStage[Search Stage]
            GatherStage[Gather Stage]
            CheckStage[Check Stage]
            InspectStage[Inspect Stage]
        end
    end

    %% Search Provider Ecosystem
    subgraph ProviderEcosystem["Search Provider Ecosystem"]
        ProviderRegistry[Provider Registry]
        BaseProvider[Base Provider]
        OpenAIProvider[OpenAI-like Provider]
        CustomProviders[Custom Providers]
    end

    %% Advanced Processing Engines
    subgraph ProcessingEngines["Processing Engines"]
        SearchClient[Search Client]

        %% Query Optimization Engine
        subgraph QueryOptimizer["Query Optimization Engine"]
            RefineEngine[Refine Engine]
            RegexParser[Regex Parser]
            SplittabilityAnalyzer[Splittability Analyzer]
            EnumerationOptimizer[Enumeration Optimizer]
            QueryGenerator[Query Generator]
            OptimizationStrategies[Optimization Strategies]

            %% Internal Flow
            RefineEngine --> RegexParser
            RegexParser --> SplittabilityAnalyzer
            SplittabilityAnalyzer --> EnumerationOptimizer
            EnumerationOptimizer --> OptimizationStrategies
            OptimizationStrategies --> QueryGenerator
        end

        ValidationEngine[API Key Validation]
        RecoveryEngine[Task Recovery]
    end

    %% State & Data Management
    subgraph StateManagement["State & Data Management"]
        StateCollector[State Collector]
        DisplayEngine[Display Engine]
        StatusBuilder[Status Builder]
        StateMonitor[State Monitor]
        PersistenceLayer[Persistence Layer]
        SnapshotManager[Snapshot Manager]
        ResultManager[Result Manager]
    end

    %% Infrastructure Services
    subgraph Infrastructure["Infrastructure Services"]
        RateLimiting[Rate Limiting]
        CredentialMgmt[Credential Management]
        AgentRotation[User Agent Rotation]
        LoggingSystem[Logging System]
        RetryFramework[Retry Framework]
        ResourcePool[Resource Pool]
    end

    %% External Systems
    subgraph External["External Systems"]
        GitHubAPI[GitHub API]
        GitHubWeb[GitHub Web Interface]
        AIServiceAPIs[AI Service APIs]
        FileSystem[Local File System]
    end

    %% User Interactions
    User --> CLI
    User --> ConfigMgmt
    CLI --> MainApp
    ConfigMgmt --> MainApp

    %% Application Flow
    MainApp --> TaskManager
    MainApp --> StatusManager
    MainApp --> ResourceManager
    MainApp --> ShutdownManager
    TaskManager --> StageRegistry
    TaskManager --> QueueManager

    %% Stage Management Flow
    StageRegistry --> DependencyResolver
    StageRegistry --> StageFactory
    DependencyResolver --> ProcessingStages
    StageFactory --> ProcessingStages

    %% Queue Management Flow
    QueueManager --> WorkerManager
    QueueManager --> MonitoringSystem
    WorkerManager --> ProcessingStages

    %% Stage Dependencies (Pipeline)
    SearchStage --> GatherStage
    GatherStage --> CheckStage
    CheckStage --> InspectStage

    %% Processing Engine Integration
    SearchStage --> SearchClient
    SearchStage --> QueryOptimizer
    CheckStage --> ValidationEngine
    ProcessingStages --> RecoveryEngine

    %% Provider Integration
    SearchClient --> ProviderRegistry
    ProviderRegistry --> BaseProvider
    BaseProvider --> OpenAIProvider
    BaseProvider --> CustomProviders

    %% State Management Integration
    ProcessingStages --> StateCollector
    QueueManager --> StateCollector
    StateCollector --> DisplayEngine
    StateCollector --> StatusBuilder
    StateMonitor --> DisplayEngine
    ProcessingStages --> PersistenceLayer
    PersistenceLayer --> SnapshotManager
    PersistenceLayer --> ResultManager

    %% Infrastructure Integration
    SearchClient -.-> RateLimiting
    ResourceManager -.-> CredentialMgmt
    ResourceManager -.-> AgentRotation
    MainApp -.-> LoggingSystem
    ProcessingStages -.-> RetryFramework
    Infrastructure -.-> ResourcePool

    %% External Connections
    SearchClient --> GitHubAPI
    SearchClient --> GitHubWeb
    ValidationEngine --> AIServiceAPIs
    PersistenceLayer --> FileSystem

    %% Styling
    classDef userClass fill:#e3f2fd,stroke:#1976d2,stroke-width:2px
    classDef appClass fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px
    classDef coreClass fill:#e8f5e8,stroke:#388e3c,stroke-width:3px
    classDef providerClass fill:#fff3e0,stroke:#f57c00,stroke-width:2px
    classDef engineClass fill:#fce4ec,stroke:#c2185b,stroke-width:2px
    classDef stateClass fill:#f1f8e9,stroke:#689f38,stroke-width:2px
    classDef infraClass fill:#f5f5f5,stroke:#616161,stroke-width:2px
    classDef externalClass fill:#ffebee,stroke:#d32f2f,stroke-width:2px

    class User,CLI,ConfigMgmt userClass
    class MainApp,TaskManager,StatusManager,ResourceManager,ShutdownManager appClass
    class StageRegistry,DependencyResolver,StageFactory,QueueManager,WorkerManager,MonitoringSystem,SearchStage,GatherStage,CheckStage,InspectStage coreClass
    class ProviderRegistry,BaseProvider,OpenAIProvider,CustomProviders providerClass
    class SearchClient,QueryOptimizer,ValidationEngine,RecoveryEngine engineClass
    class StateCollector,StateMonitor,DisplayEngine,StatusBuilder,PersistenceLayer,SnapshotManager,ResultManager stateClass
    class RateLimiting,CredentialMgmt,AgentRotation,LoggingSystem,RetryFramework,ResourcePool infraClass
    class GitHubAPI,GitHubWeb,AIServiceAPIs,FileSystem externalClass
```

The project follows a layered architecture with the following core components:

### Multi-Stage Processing Flow

```mermaid
sequenceDiagram
    participant CLI as CLI
    participant App as Application
    participant TM as TaskManager
    participant Pipeline as Pipeline
    participant Search as SearchStage
    participant Gather as GatherStage
    participant Check as CheckStage
    participant Inspect as InspectStage
    participant Storage as Storage
    participant Monitor as StatusManager

    %% Initialization Phase
    CLI->>App: 1. Start Application
    App->>App: 2. Load Configuration
    App->>TM: 3. Create TaskManager
    TM->>TM: 4. Initialize Providers
    TM->>Pipeline: 5. Create Pipeline
    Pipeline->>Search: 6. Register SearchStage
    Pipeline->>Gather: 7. Register GatherStage
    Pipeline->>Check: 8. Register CheckStage
    Pipeline->>Inspect: 9. Register InspectStage
    App->>Monitor: 10. Start Status Manager

    %% Processing Phase
    loop Multi-Stage Processing
        TM->>Search: 11. Submit Search Tasks
        Search->>Search: 12. Query GitHub with Optimization
        Search->>Gather: 13. Forward Search Results

        Gather->>Gather: 14. Acquire Detailed Information
        Gather->>Check: 15. Forward Extracted Keys

        Check->>Check: 16. Validate API Keys
        Check->>Inspect: 17. Forward Valid Keys

        Inspect->>Inspect: 18. Inspect API Capabilities
        Inspect->>Storage: 19. Save Results

        Pipeline->>Monitor: 20. Update Status
        Monitor->>App: 21. Display Progress
    end

    %% Recovery and Persistence
    loop Background Operations
        Storage->>Storage: Auto-save Results
        Storage->>Storage: Create Snapshots
        Pipeline->>Pipeline: Task Recovery
        Monitor->>Monitor: Collect Metrics
    end

    %% Completion Phase
    Pipeline->>Pipeline: 22. Check Completion
    Pipeline->>Storage: 23. Final Persistence
    Pipeline->>Monitor: 24. Final Status Report
    App->>TM: 25. Graceful Shutdown
    TM->>Storage: 26. Save State
```

## Architecture Layers

### 1. **Presentation Layer**
   - **CLI Interface** (`main.py`): Command-line entry point with argument parsing and application lifecycle
   - **Configuration System** (`config/`): YAML-based configuration management with validation and schemas

### 2. **Application Layer**
   - **Application Core** (`main.py`): Main application lifecycle and orchestration
   - **Task Management** (`manager/task.py`): Provider coordination and task distribution
   - **Resource Coordination** (`tools/coordinator.py`): Global resource management and coordination
   - **Shutdown Management** (`manager/shutdown.py`): Graceful shutdown coordination
   - **Status Management** (`manager/status.py`): Application status management and coordination
   - **Worker Management** (`manager/worker.py`): Worker thread management and scaling
   - **Queue Management** (`manager/queue.py`): Multi-queue coordination and management

### 3. **Business Service Layer**
   - **Pipeline Engine** (`manager/pipeline.py`): Multi-stage processing orchestration with DAG execution
   - **Stage System** (`stage/`): Pluggable processing stages with dependency resolution and factory pattern
   - **Search Service** (`search/`): GitHub code search with provider abstraction and optimization
   - **Query Refinement** (`refine/`): Intelligent query optimization with strategy pattern and mathematical foundations

### 4. **Domain Layer**
   - **Core Models & Tasks** (`core/models.py`): Business domain objects, data structures, and task definitions
   - **Type System** (`core/types.py`): Interface definitions and contracts
   - **Business Enums** (`core/enums.py`): Domain enumerations and constants
   - **Metrics & Analytics** (`core/metrics.py`): Performance measurement and KPI tracking
   - **Authentication** (`core/auth.py`): Authentication and authorization logic
   - **Custom Exceptions** (`core/exceptions.py`): Domain-specific exception handling
   - **Custom Exceptions** (`core/exceptions.py`): Domain-specific exception handling

### 5. **Infrastructure Layer**
   - **Storage & Persistence** (`storage/`): Result storage, recovery, and snapshot management
     - **Atomic Operations** (`storage/atomic.py`): Atomic file operations with fsync
     - **Result Management** (`storage/persistence.py`): Multi-format result persistence
     - **Task Recovery** (`storage/recovery.py`): Task recovery mechanisms
     - **Shard Management** (`storage/shard.py`): NDJSON shard management with rotation
     - **Snapshot Management** (`storage/snapshot.py`): Backup and restore functionality
   - **Tools & Utilities** (`tools/`): Infrastructure tools and utilities
     - **Logging System** (`tools/logger.py`): Structured logging with API key redaction
     - **Rate Limiting** (`tools/ratelimit.py`): Adaptive rate control with token bucket algorithm
     - **Load Balancing** (`tools/balancer.py`): Resource distribution strategies
     - **Credential Management** (`tools/credential.py`): Secure credential rotation and management
     - **Agent Management** (`tools/agent.py`): User-agent rotation for web scraping
     - **Pattern Matching** (`tools/patterns.py`): Pattern matching utilities and helpers
     - **Retry Framework** (`tools/retry.py`): Unified retry mechanisms with backoff strategies
     - **Resource Pooling** (`tools/resources.py`): Resource pool management and optimization

### 6. **State Management Layer**
   - **State Collection** (`state/collector.py`): System metrics gathering and aggregation
   - **Display Engine** (`state/display.py`): User-friendly progress visualization and formatting
   - **Status Builder** (`state/builder.py`): Status data construction and transformation
   - **State Models** (`state/models.py`): Monitoring data structures and metrics
   - **State Monitoring** (`state/monitor.py`): Real-time state monitoring and tracking
   - **State Enumerations** (`state/enums.py`): State-related enumerations and constants
   - **State Types** (`state/types.py`): State type definitions and interfaces


## Processing Stages

The system implements a **4-stage pipeline** for comprehensive data acquisition and validation:

1. **Search Stage** (`stage/definition.py:SearchStage`):
   - Intelligent GitHub code search with advanced query optimization
   - Multi-provider search support (API + Web)
   - Query refinement using mathematical optimization algorithms
   - Rate-limited search execution with adaptive throttling

2. **Gather Stage** (`stage/definition.py:GatherStage`):
   - Detailed information acquisition from search results
   - Content extraction and parsing
   - Pattern matching for key identification
   - Structured data collection and normalization

3. **Check Stage** (`stage/definition.py:CheckStage`):
   - API key validation against actual service endpoints
   - Authentication verification and capability testing
   - Service availability and response validation
   - Error handling and retry mechanisms

4. **Inspect Stage** (`stage/definition.py:InspectStage`):
   - API capability inspection for validated keys
   - Model enumeration and feature detection
   - Service limits and quota analysis
   - Comprehensive capability profiling

## Advanced Query Optimization Engine

The system features a sophisticated **Query Optimization Engine** with mathematical foundations:

### Core Components

1. **Regex Parser**
   - Advanced regex pattern parsing with support for complex syntax
   - Handles escaped characters, character classes, and quantifiers
   - Converts patterns into analyzable segment structures

2. **Splittability Analyzer**
   - Mathematical analysis of pattern divisibility
   - Recursive depth limiting for safety
   - Value threshold analysis for optimization feasibility
   - Resource cost estimation for performance control

3. **Enumeration Optimizer**
   - Intelligent enumeration strategy selection
   - Multi-dimensional optimization (depth, breadth, value)
   - Combinatorial analysis for optimal segment selection
   - Topological sorting for dependency resolution

4. **Query Generator**
   - Generates optimized query variants from enumeration strategies
   - Supports configurable enumeration depth
   - Produces mathematically optimal query distributions
   - Maintains query semantic equivalence

### Optimization Algorithms

- **Mathematical Modeling**: Uses mathematical principles to analyze regex patterns
- **Enumeration Strategy**: Intelligent selection of optimal enumeration depth and combinations
- **Resource Management**: Prevents resource exhaustion through intelligent limiting
- **Performance Optimization**: Singleton pattern ensures optimal memory usage

### API Query Cleaning (`clean_regex`)

Before an API-transport condition is sent, `RefineEngine.clean_regex` reduces
`/regex/` parts to their quoted fixed literals (GitHub's REST code search does
not accept regex). Cleaning is **search-syntax-aware**: the query is tokenized
into escape-aware `/regex/` spans, double-quoted literals, `qualifier:value`
tokens (`filename:.env`, `repo:owner/name`), boolean operators and bare words.
Fixed-string extraction applies only to genuine `/regex/` spans; quoted literals
and qualifiers pass through verbatim, so `"sk-" filename:.env` is sent unchanged
instead of being mangled into a zero-result wire query.

- **Stability contract** — already-correct forms stay byte-identical
  (`"sk-"` → `"sk-"`, `filename:.env` → `filename:.env`,
  `/sk-[a-zA-Z0-9]{32}/` → `"sk-"`, bare `AKIA` → `"AKIA"`); the
  search-aggregation golden vectors and the `_preprocess_query == wire_query`
  pin enforce it.
- **Composition** — `search/querykey.py::wire_query` delegates to `clean_regex`,
  so wire forms, fingerprints, cache keys and queue locality inherit the fix
  with no extra code.
- **Fail-open fallback** — a remnant the tokenizer cannot classify (e.g. an
  unbalanced quote) is emitted verbatim with a counted warning
  (`RefineEngine._remnant_count`); the regex parser never runs over plain text.

## Supported Data Sources & Use Cases

### 🔍 Current Implementation (AI Service Discovery)
- **OpenAI and compatible interfaces**
- **Anthropic Claude**
- **Azure OpenAI**
- **Google Gemini**
- **AWS Bedrock**
- **GooeyAI**
- **Stability AI**
- **百度文心一言**
- **智谱AI**
- **Custom providers**

### 🌐 Planned Data Sources
- **[FOFA](https://fofa.info)**: Cyberspace asset discovery and network mapping
- **[Shodan](https://www.shodan.io/)**: Internet-connected device enumeration
- **Custom REST APIs**: Generic API integration framework
- **GraphQL Endpoints**: Flexible query-based data acquisition
- **Web Scraping**: JavaScript-rendered content and dynamic sites
- **Database Connectors**: Direct database query capabilities

### 📊 Potential Use Cases
- **Data Mining**: Large-scale information extraction and analysis

## Key Features

### 🌐 Universal Data Acquisition
- **Multi-Source Support**: GitHub, FOFA, Shodan, and custom endpoints
- **Adaptive Query Engine**: Intelligent optimization for different data sources
- **Protocol Agnostic**: REST, GraphQL, WebSocket, and web scraping support
- **Rate Limiting**: Per-source intelligent rate control and quota management

### 🏗️ Advanced Architecture
- **Dynamic Stage System**: Configurable processing pipelines with DAG execution
- **Plugin Architecture**: Extensible framework for custom data sources and processors
- **Dependency Resolution**: Automatic stage ordering and dependency management
- **Handler Registration**: Pluggable processors for flexible data transformation

### ⚡ High Performance
- **Asynchronous Processing**: Multi-threaded task execution with intelligent queuing
- **Adaptive Load Balancing**: Dynamic resource allocation based on workload
- **Query Optimization**: Mathematical modeling for optimal search strategies
- **Resource Monitoring**: Real-time performance tracking and bottleneck detection

### 🛡️ Enterprise Ready
- **Fault Tolerance**: Comprehensive error handling, retry mechanisms, and recovery
- **State Persistence**: Queue state recovery and graceful shutdown capabilities
- **Security**: Credential management, API key redaction, and secure storage
- **Monitoring**: Real-time analytics, alerting, and performance visualization

## System Requirements

### **Dependencies**
- **Python**: 3.10+
- **Libraries**: `PyYAML`
- **Optional**: `uvloop` (Linux/macOS performance boost)
- **Development**: `pytest`, `black`, `mypy` (for contributors)

## Quick Start

> 📚 For comprehensive documentation, tutorials, and advanced usage guides, please visit [DeepWiki](https://deepwiki.com/wzdnzd/harvester)

1. **Installation**
   ```bash
   git clone https://github.com/wzdnzd/harvester.git
   cd harvester
   pip install -r requirements.txt
   ```

2. **Configuration**

  > Choose one of the following methods to create your configuration

   **Method 1: Generate default configuration**
   ```bash
   python main.py --create-config
   ```

   **Method 2: Copy from examples**
   ```bash
   # For basic configuration
   cp examples/config-simple.yaml config.yaml

   # For full configuration with all options
   cp examples/config-full.yaml config.yaml
   ```

   Edit the configuration file:
   - Set your Github session token or API key
   - Configure provider search patterns
   - Adjust rate limits and thread counts

   ### Configuration Guide

   The system provides two configuration templates:

   1. **Basic Configuration** - Suitable for quick start:
      ```yaml
      # Global application settings
      global:
        workspace: "./data"  # Working directory
        github_credentials:
          sessions:
            - "your_github_session_here"  # GitHub session token
          strategy: "round_robin"  # Load balancing strategy

      # Pipeline stage configuration
      pipeline:
        threads:
          search: 1    # Search threads (keep low)
          gather: 4   # Acquisition threads
          check: 2     # Validation threads
          inspect: 1    # API capability inspection threads

      # System monitoring settings
      monitoring:
        update_interval: 2.0    # Monitoring update interval
        error_threshold: 0.1    # Error rate threshold

      # Data persistence configuration
      persistence:
        auto_restore: true      # Auto restore state on startup
        shutdown_timeout: 30    # Shutdown timeout in seconds

      # Global rate limiting configuration
      ratelimits:
        github_web:
          base_rate: 0.5       # Base rate in requests per second
          burst_limit: 2       # Maximum burst size
          adaptive: true       # Enable adaptive rate limiting

      # Provider task configurations
      tasks:
        - name: "openai"         # Provider name
          enabled: true          # Enable/disable provider
          provider_type: "openai"
          use_api: false         # Use GitHub API for searching
          
          # Pipeline stage settings
          stages:
            search: true         # Enable search stage
            gather: true         # Enable acquisition stage
            check: true          # Enable validation stage
            inspect: true        # Enable API capability inspection
          
          # Pattern matching configuration
          patterns:
            key_pattern: "sk(?:-proj)?-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20}"
          
          # Search conditions
          conditions:
            - query: '"T3BlbkFJ"'
      ```

   2. **Full Configuration** - Includes all advanced options:
      - `display`: Display and monitoring settings
      - `global`: Global system configuration
      - `pipeline`: Pipeline stage configuration
      - `monitoring`: System monitoring parameters
      - `persistence`: Data persistence settings
      - `worker`: Worker pool configuration
      - `ratelimits`: Rate limiting settings
      - `tasks`: Provider task configurations

   ### Advanced Task Configuration

   > 📋 **For complete configuration examples, please refer to:**
   > - [`examples/config-full.yaml`](examples/config-full.yaml) - Comprehensive configuration with all available options
   > - [`examples/config-simple.yaml`](examples/config-simple.yaml) - Basic configuration for quick start

   The `tasks` section is the core of the configuration, defining what providers to search and how to process them. Refer to the basic configuration example above for a complete tasks configuration.

   #### Key Configuration Options

   - **`name`**: Unique identifier for the task
   - **`provider_type`**: Determines validation method (`openai`, `openai_like`, `anthropic`, `gemini`, etc.)
   - **`api`**: API endpoint configuration for key validation
   - **`patterns.key_pattern`**: Regex pattern to identify valid API keys
   - **`conditions`**: Search queries to find potential keys
   - **`stages`**: Enable/disable specific processing stages
   - **`extras.directory`**: Custom output directory for results

3. **Running**
   ```bash
   python main.py                  # Use default config
   python main.py -c custom.yaml   # Use custom config
   python main.py --validate       # Validate config
   python main.py --log-level DEBUG # Enable debug logging
   ```

## Directory Structure

```
harvester/
├── config/           # Configuration management
│   ├── accessor.py   # Configuration access utilities
│   ├── defaults.py   # Default configuration values
│   ├── loader.py     # Configuration loading
│   ├── schemas.py    # Configuration schemas
│   ├── validator.py  # Configuration validation
│   └── __init__.py   # Package initialization
├── constant/         # System constants
│   ├── monitoring.py # Monitoring constants
│   ├── runtime.py    # Runtime constants
│   ├── search.py     # Search constants
│   ├── system.py     # System constants
│   └── __init__.py   # Package initialization
├── core/             # Core domain models
│   ├── auth.py       # Authentication
│   ├── enums.py      # System enumerations
│   ├── exceptions.py # Custom exceptions
│   ├── metrics.py    # Performance metrics
│   ├── models.py     # Core data models & task definitions
│   ├── types.py      # Core type definitions
│   └── __init__.py   # Package initialization
├── examples/         # Configuration examples
│   ├── config-full.yaml    # Complete configuration template
│   └── config-simple.yaml  # Basic configuration template
├── manager/          # Task and resource management
│   ├── base.py       # Base management classes
│   ├── pipeline.py   # Pipeline management
│   ├── queue.py      # Queue management
│   ├── shutdown.py   # Shutdown coordination
│   ├── status.py     # Status management
│   ├── task.py       # Task management
│   ├── worker.py     # Worker thread management
│   └── __init__.py   # Package initialization
├── refine/           # Query optimization
│   ├── config.py     # Refine configuration
│   ├── engine.py     # Optimization engine
│   ├── generator.py  # Query generation
│   ├── optimizer.py  # Query optimization
│   ├── parser.py     # Query parsing
│   ├── segment.py    # Pattern segmentation
│   ├── splittability.py # Splittability analysis
│   ├── strategies.py # Optimization strategies
│   ├── types.py      # Refine type definitions
│   └── __init__.py   # Package initialization
├── search/           # Search engines
│   ├── client.py     # Search client
│   ├── provider/     # Provider implementations
│   │   ├── anthropic.py    # Anthropic provider
│   │   ├── azure.py        # Azure OpenAI provider
│   │   ├── base.py         # Base provider class
│   │   ├── bedrock.py      # AWS Bedrock provider
│   │   ├── doubao.py       # ByteDance Doubao provider
│   │   ├── gemini.py       # Google Gemini provider
│   │   ├── gooeyai.py      # GooeyAI provider
│   │   ├── openai.py       # OpenAI provider
│   │   ├── openai_like.py  # OpenAI-compatible provider
│   │   ├── qianfan.py      # Baidu Qianfan provider
│   │   ├── registry.py     # Provider registry
│   │   ├── stabilityai.py  # Stability AI provider
│   │   ├── vertex.py       # Google Vertex AI provider
│   │   └── __init__.py     # Package initialization
│   └── __init__.py   # Package initialization
├── stage/            # Pipeline stages
│   ├── base.py       # Base stage classes
│   ├── definition.py # Stage implementations
│   ├── factory.py    # Stage factory
│   ├── registry.py   # Stage registry
│   ├── resolver.py   # Dependency resolver
│   └── __init__.py   # Package initialization
├── state/            # State management
│   ├── builder.py    # Status builder
│   ├── collector.py  # State collection
│   ├── display.py    # Display engine
│   ├── enums.py      # State enumerations
│   ├── models.py     # State data models
│   ├── monitor.py    # State monitoring
│   ├── types.py      # State type definitions
│   └── __init__.py   # Package initialization
├── storage/          # Storage and persistence
│   ├── atomic.py     # Atomic file operations
│   ├── persistence.py # Result persistence
│   ├── recovery.py   # Task recovery
│   ├── shard.py      # NDJSON shard management
│   ├── snapshot.py   # Snapshot management
│   └── __init__.py   # Package initialization
├── tools/            # Tools and utilities
│   ├── agent.py      # User agent management
│   ├── balancer.py   # Load balancing
│   ├── coordinator.py # Resource coordination
│   ├── credential.py # Credential management
│   ├── logger.py     # Logging system
│   ├── patterns.py   # Pattern matching utilities
│   ├── ratelimit.py  # Rate limiting
│   ├── resources.py  # Resource pooling
│   ├── retry.py      # Retry framework
│   ├── utils.py      # General utilities
│   └── __init__.py   # Package initialization
├── .dockerignore     # Docker ignore rules
├── .gitignore        # Git ignore rules
├── Dockerfile        # Docker container configuration
├── entrypoint.sh     # Docker entrypoint script
├── LICENSE           # License file
├── main.py           # Entry point and application core
├── README.md         # English documentation
├── README.zh-CN.md   # Chinese documentation
├── requirements.txt  # Python dependencies
└── __init__.py       # Root package initialization
```

## Advanced Features

1. **Real-time Monitoring**
   - Task status tracking
   - Performance metrics collection
   - Resource usage monitoring
   - Alert system

2. **Configuration Flexibility**
   - Multi-provider configuration
   - Custom search patterns
   - Adjustable performance parameters
   - Dynamic resource allocation

3. **Extensibility**
   - Plugin-style providers
   - Custom pipeline stages
   - Configurable monitoring system
   - Flexible recovery strategies

## Persistent Link Registry

The registry is a durable, cross-provider SQLite ledger of every GitHub link
the harvester has discovered, which provider/pattern set gathered it, and a
journal of each run. It gives the pipeline memory across restarts and provider
changes. **By default it is write-only:** with recording enabled and
`skip.skip_known: off`, nothing reads it to influence task creation, so
enabling recording does not change the produced shards. The opt-in gather-skip
feature below is the first read path.

- **Location:** `<workspace>/registry.sqlite` (plus WAL `-wal`/`-shm` sidecars),
  outside `providers/<folder>/`, so it survives provider-set changes.
- **Journal mode:** WAL. The workspace must live on a **local filesystem**;
  SQLite WAL is not safe over network filesystems (NFS/SMB).
- **Disk estimate:** roughly 200 bytes per link row — about 200 MB for 1M links.

### Two storage layers: shards + registry

Link discovery is recorded twice, by design:

1. **NDJSON shards (audit log, per provider).**
   `<workspace>/providers/<folder>/shards/links/links_<YYYYMMDD_HHMMSS_mmm>.ndjson`
   — append-only, one JSON record per line in a `{"value": "<url>"}` envelope,
   each shard accompanied by an atomic `.index.json` sidecar. Old shards are
   never deleted; on restart `recover_tasks()` replays them into acquisition
   tasks. This layer is the immutable discovery trail.
2. **SQLite registry (global ledger).** The single `<workspace>/registry.sqlite`
   outside `providers/`, which deduplicates and enriches everything the shards
   record. Tables: `links` (one row per canonical URL — hash, owner/repo/path,
   first-seen/last-seen/gathered timestamps, `visit_status`, transport,
   freshness columns, priority), `link_coverage` (which provider + pattern set
   already gathered the link), `keys` (provider-scoped key ledger), `repos`
   (metadata cache), `runs` (run journal with config digests), `meta`.

The registry upserts on every encounter (`last_seen_ts` advances, history is
preserved), survives process restarts and abrupt kills via WAL, and drains its
writer queue on graceful shutdown.

### Provider switches

Links are provider-independent artifacts, so the registry is global while
provider attribution lives in columns and tables — switching the provider set
between runs loses nothing:

- Run under **provider A**: the link is recorded (`provider=A` as discoverer);
  a successful gather writes `visit_status='gathered_ok'` plus a coverage row
  `(url, A, patterns_hash_A)`.
- Later run under **provider B**: the same URL is upserted — dates, timestamps
  and history preserved — but gather-skip condition 4 finds no coverage row for
  `(B, patterns_hash_B)`, so the link is gathered **once** under B's patterns
  (`regathered_coverage_gap`). The re-gather is necessary, not wasteful:
  extraction runs per-provider regexes, so A's pass never saw B's keys. After
  success, both coverage rows coexist and either provider skips the link within
  its TTL.
- Keys never collide across providers: `key_hash = sha256(provider|key|address|endpoint)`.
- The `runs` journal keeps which run used which providers/patterns
  (`config_digest`), so cross-provider history stays auditable.

### Configuration

```yaml
registry:
  enabled: false      # default; true turns recording on (no behavior change)
  path: ""            # empty => <workspace>/registry.sqlite
  batch_size: 50      # rows per batched upsert
  flush_interval: 5   # seconds between periodic flushes
  queue_size: 100000  # bounded queue; overflow drops writes and degrades the run
```

When `enabled: false` (the default) no registry file is created or opened and
all write hooks are no-ops.

### Migration from existing workspaces

The one-time migration imports historical shards into the registry. It is
idempotent and never overwrites fresher in-place state. Links are imported
conservatively (`visit_status='discovered'`, `gathered_ts=NULL`) because legacy
shards cannot distinguish "seen" from "gathered"; the worst case is one extra
re-gather wave once skip features land.

```bash
# Report what would be imported, write nothing
python -m tools.registry_migrate --workspace ./data --dry-run

# Perform the migration
python -m tools.registry_migrate --workspace ./data

# Explicit registry path
python -m tools.registry_migrate --workspace ./data --registry /path/registry.sqlite
```

### Operations

- **Rollback:** set `registry.enabled: false`. Deleting `registry.sqlite*` is
  harmless while the registry is write-only.
- **Space reclamation:** after large migrations, run `VACUUM` manually:
  `sqlite3 <workspace>/registry.sqlite "VACUUM;"`. Not automated.
- **Integrity check:** `sqlite3 <workspace>/registry.sqlite "PRAGMA integrity_check;"`
  passes after abrupt termination because committed batches are WAL-recovered.
- **Degraded runs:** any registry error logs a warning, marks the run degraded
  in `runs.degraded`, and continues harvesting unchanged.

### Date extraction (free freshness metadata)

While the registry is enabled, the harvester also fills the reserved
freshness columns from payloads it already downloaded — no extra requests:

- **API search** records `repository.pushed_at` (fallback `updated_at`) and
  `repository.size` (`repo_pushed_at`, `repo_size_kb`).
- **Gather** records the latest `<relative-time datetime="…">` on the blob
  page (`file_commit_date`).

Transport asymmetry is deliberate: web **search-results** HTML is not parsed
for dates, so web-discovered links stay NULL until gathered. The file date uses
the **maximum** of all matches: overestimating can only cause a cheap re-gather,
whereas underestimating could cause a dangerous false skip. Merge is
non-regressing — NULL never overwrites a known value.

Per-run fill rates `date_fill_rate_api` and `date_fill_rate_web` are exposed in
`PipelineStatus.date_metrics`. Both carry a documented **0.0 baseline** (live
probe, September 2026 — design D7): GitHub serves trimmed `repository` objects
in `/search/code` and renders blob timestamps client-side, so a sustained
*rise* is the signal that GitHub restored the fields or that
`add-repo-meta-enrichment` began feeding the columns.
See `docs/specs/date_extraction.md`.

See `docs/specs/url_canonicalization.md` (identity canon) and
`docs/specs/registry_flags_metrics.md` (flag matrix and metrics dictionary).

### Gather-skip (`skip.skip_known`)

Optional, default-off suppression of acquisition tasks for links the harvester
already researched under the current provider/pattern set. It needs
`registry.enabled: true` to have data to read.

```yaml
skip:
  skip_known: "off"      # off | shadow | on
  gather_ttl_hours: 168  # 7 days
```

An acquisition task is skipped **only when all four conditions hold**; the
first failing condition names the `regathered_*` counter:

| # | Condition | On failure |
|---|-----------|------------|
| 1 | `visit_status = 'gathered_ok'` | `failed` → `regathered_failed_retry`; never gathered → task, no reason |
| 2 | `gathered_ts` within `gather_ttl_hours` | `regathered_ttl_expired` |
| 3 | no change evidence: `repo_pushed_at` is NULL **or** not later than `gathered_ts` (+60 s grace) | `regathered_changed` |
| 4 | a `link_coverage` row exists for the current `(provider, patterns_hash)` | `regathered_coverage_gap` |

Any read error or missing row makes the link **unknown** ⇒ the task is created
(fail-open: absence of evidence never causes a skip). A NULL `repo_pushed_at`
passes condition 3 vacuously — with GitHub dates absent fleet-wide this is the
prevailing mode, and `push_signal_coverage` makes the evidence quality visible.

**Modes**
- `off` (default): no registry reads during search; behavior is identical to
  pre-change.
- `shadow`: every would-skip decision is appended to
  `<workspace>/registry_decisions.jsonl` and **all** acquisition tasks are
  still created, so the false-skip price can be measured before enforcement.
- `on`: computed skips are enforced.

The `links` shard records **every** search hit in every mode; only task
creation is suppressed, so the discovery audit trail is unaffected. Per-run
counters `skipped_known`, `regathered_{changed,coverage_gap,ttl_expired,failed_retry}`
and `push_signal_coverage` are exposed in `PipelineStatus.skip_metrics` and the
status display.

**Decision-log growth:** `registry_decisions.jsonl` is append-only (one line
per would-skip). It is pure observability data — rotate, truncate or archive it
freely between cycles; the run counters do not depend on the file.

**Rollback:** set `skip.skip_known: off` — a config flip, no code removal. A run
whose registry was degraded is not a trustworthy source of "known" counts.

**Promotion `shadow` → `on`:** run at least one representative full cycle in
`shadow`, join the decision log against that run's `material`/`valid` shards,
and require the false-skip price (keys found at would-skipped URLs) ≈ 0 before
flipping. Repeat the gate on a larger representative cycle before production
rollout — while `push_signal_coverage` is near 0 the measurement validates the
TTL-only regime only. The step-by-step procedure lives in
`docs/specs/registry_flags_metrics.md`.

### Repository metadata enrichment (`enrichment.enabled`)

Optional, default-off restoration of repository-level freshness evidence that
the September 2026 probe lost to trimmed search payloads. It fetches
`GET /repos/{owner}/{repo}` through the shared `github_api` client (adaptive
bucket, cooldown, credential rotation, `Retry-After`/`X-RateLimit-Reset`) and
caches the result in the registry's `repos` table, which doubles as the work
ledger. It needs `registry.enabled: true` for a durable cache.

```yaml
enrichment:
  enabled: false        # default off; byte-for-byte inert when off
  ttl_hours: 24         # cache freshness before a conditional refresh
```

**Quota model.** Cost is bounded by unique repositories, not by links:

- First sighting of a repository in a run: at most **one request per unique
  `(owner, repo)` per TTL window**, regardless of how many links point at it
  (duplicate encounters coalesce to one fetch).
- A TTL-expired entry is refreshed conditionally with
  `If-None-Match: <stored etag>`. An unchanged repo answers `304 Not Modified`:
  GitHub charges **zero** rate-limit units, only `fetched_at` advances, and no
  link merge runs. Only a genuine `200` (repo changed) replaces values and
  propagates `pushed_at`/`size_kb` into the repository's `links` rows via the
  existing UPDATE-only COALESCE channel.
- A repository that was taken down answers `404`: it is marked `gone=1`,
  retries are suppressed within TTL, and existing link rows are preserved (no
  silent cleanup). The `gone` flag is the forward contract for candidate export
  so dangling commits stay targetable.
- Transient failures (network/5xx/credential exhaustion) degrade **fail-open**:
  link dates stay NULL, a warning is logged once, and the pipeline proceeds.

The steady-state cost therefore decays toward one cheap conditional request per
unique repository per TTL window, with 304s dominating once the corpus is warm.

**Tokenless asymmetry.** Enrichment needs an API token; web sessions cannot
call the REST endpoint. With no usable token the enricher silently
self-disables — zero requests, dates stay NULL, at most one informational log
line, and the web-only pipeline is unaffected. When every API token is cooling
down, enrichment transiently yields instead of blocking (zero requests,
`cooling_skips` counter, one info log) and resumes within the same run once a
token recovers — search keeps its own blocking cooldown policy untouched.

**Metrics.** `enrichment_fetches`, `enrichment_304s`, `enrichment_failures`,
`repos_cached` and `gone` are exposed in `PipelineStatus.enrichment_metrics` and
the status display when enabled (nothing is emitted while `off`).

**Rollback:** set `enrichment.enabled: false` — a config flip; the `repos`
cache simply stops being consulted and no code is removed.

See `docs/specs/registry_flags_metrics.md` for the flag/metrics dictionary and
the enrichment semantics.

### API pagination early-stop (`early_stop.mode`)

Optional, default-off termination of deeper API pagination once a refined
partition's result stream is saturated with already-researched links. Because
GitHub's `sort=indexed&order=desc` returns freshly-indexed files first, once the
frontier (the boundary between new and already-researched content) is behind the
window, deeper pages are re-runs of the past — a repeat run becomes delta
collection. It needs `registry.enabled: true` to classify links and to pass the
trust gate.

```yaml
early_stop:
  mode: "off"      # off | shadow | on
  window: 100      # trailing result identities in the ratio window
  theta: 0.9       # known-ratio threshold in [0.5, 1.0]
  min_pages: 2     # never stop on the first page(s)
  min_trust: 1000  # minimum links rows before the trust gate can pass
```

**Four independent gates.** A partition stops only when ALL hold, so every
failure mode is biased toward a full pass:

1. **Ratio** — the trailing window of `window` de-duplicated result identities
   is at least `theta` known.
2. **Page floor** — at least `min_pages` pages were fetched (never page 1).
3. **Trust gate** — `COUNT(links) >= min_trust`, the one-time
   `tools.registry_migrate` completed (`meta.migration_complete`), and the run
   is not degraded.
4. **Kill-switch** — any registry read/write error during the run mutes
   stopping until the run ends.

Gate 3 needs the one-time migration marker. On a fresh workspace run
`python -m tools.registry_migrate --workspace ./data` once (it is idempotent and
harmless with nothing to import) so `meta.migration_complete` exists; without
it early-stop stays safely inert.

**"Known" is gather-skip.** A result counts as known only when the amended
gather-skip conjunction would skip it (`gathered_ok` ∧ within
`skip.gather_ttl_hours` ∧ no push invalidation ∧ coverage for the current
provider/patterns). Discovered-only, foreign-coverage, TTL-expired and failed
links all count as novel, so a corpus due for a re-gather keeps early-stop
inert while that work is genuinely needed.

**Modes**
- `off` (default): the detector never evaluates; pagination is byte-for-byte
  unchanged.
- `shadow`: each partition's first saturation point is logged as a
  `type: "early_stop"` record in `<workspace>/registry_decisions.jsonl`, and
  every page is still fetched. The run then reports `novel_after_stop` — how
  many links that still require research arrived after the hypothetical stop —
  making the false-stop price a measured number before enforcement.
- `on`: computed stops are enforced; no page task is emitted beyond the stop
  point.

**API transport only.** Web results are relevance-ordered, so saturation there
carries no information; the detector is inert for web tasks at code level and
web pagination remains byte-for-byte as before regardless of the flag.

**Tuning `theta` and `window`.** `theta` (validated to `[0.5, 1.0]`) is the
aggressiveness dial: higher stops only on nearly-pure known windows, lower saves
more quota at higher false-stop risk. `window` (default 100) spans roughly one
API page, so the ratio reflects the current frontier rather than run history.
For a first rollout keep the conservative default `0.9`, promote only after a
`shadow` period shows `novel_after_stop ≈ 0` across diverse query sets, and
lower `theta` only with measurement. Per-provider tuning is safe — both are
plain config.

Per-run counters `early_stop_would_fire`, `early_stop_fired` and
`novel_after_stop` are exposed in `PipelineStatus.early_stop_metrics` and the
status display (`shadow`/`on` only).

**Rollback:** set `early_stop.mode: shadow` or `off` — a config flip, no code
removal. A run whose registry was degraded is not a trustworthy source of
"known" counts, so early-stop is muted for that run. The promotion gate
procedure lives in `docs/specs/registry_flags_metrics.md`.

### Key ledger (`check_skip`, `recheck`)

Links are the unit of discovery; **keys are the unit of value** — and the
expensive one, because every check is a real billable call to a third-party
provider. The registry's `keys` table is a persistent "two-ledger" memory:
alongside the link ledger, it records each credential's identity and last
verification status, so a repeat run stops re-verifying keys it already knows
are fresh. This preserves the project's `--only-verified` philosophy: don't poke
dead keys repeatedly, don't hammer live ones.

**Identity and secret hygiene.** A ledger row is identified by
`key_hash = sha256("provider|key|address|endpoint")` and stores only that hash
plus a masked reference (`<first6>…<last4>`). The full secret is **never**
written to the registry database, its WAL sidecars, the decision log, or any
metric — the byte-scan test (`tests/test_key_ledger.py`, scenario S9) plants
canary secrets and fails the build if one ever appears. Migrated historical keys
use the same hash/mask helpers as the runtime writer, so imported rows join
exactly.

**Inline skip (`check_skip.mode`).** With `on`, CheckStage consults the ledger
before calling the provider. A known key whose `last_recheck_ts` is inside the
TTL for its stored status is skipped: no provider call, no duplicate shard
record, and only the observation timestamp advances. Unknown hashes are always
checked, and any missing/NULL `last_recheck_ts` resolves to "check it".

| Status | Default TTL | Rationale |
|--------|-------------|-----------|
| `wait_check` | 12 h | "provider refused temporarily — retry soon" (rate-limit/no-model noise; short window absorbs flapping) |
| `no_quota` | 72 h | billing state changes on recharge (days) |
| `invalid` | 168 h | can resurrect via rotation, but rarely (a week) |
| `valid` | 336 h (14 d) | leaked cloud keys are empirically long-lived; fortnightly confirmation balances freshness against API etiquette |

TTLs are configurable per status:

```yaml
registry:
  enabled: true
check_skip:
  mode: "off"          # off | on
  ttl_hours:
    wait_check: 12
    no_quota: 72
    invalid: 168
    valid: 336
recheck:
  enabled: false       # in-process cron for TTL-expired keys
  interval_hours: 6
  batch_size: 50
```

**Periodic re-check (`recheck.enabled`).** When enabled, `RecheckManager`
(a `PeriodicTaskManager`) selects TTL-expired ledger keys — priority
`wait_check → valid → no_quota → invalid`, oldest `last_recheck_ts` first,
bounded by `batch_size` — and enqueues ordinary `CheckTask`s through the normal
CheckStage queue, so every existing provider rate limit and token bucket
applies. Because the registry stores no plaintext, the driver recovers the key
from the result shards (`ShardKeyResolver`); unresolved keys are counted and
skipped rather than guessed. Legacy migration rows with `last_recheck_ts = NULL`
count as expired and drain gradually through the batch cap.

**Metrics & rollback.** `check_skipped_by_status{valid,wait_check,invalid,no_quota}`
and `provider_calls_saved` are exposed in `PipelineStatus.key_ledger_metrics`;
`rechecks_enqueued` in `PipelineStatus.recheck_metrics`. Ledger recording starts
as soon as `registry.enabled` is true even while both flags are `off`, so the
data accumulates safely. Flip `check_skip.mode` / `recheck.enabled` back to
`off`/`false` to fully restore pre-change behavior — a config change, no code.

**Fail-open.** Any ledger read/write error degrades to the pre-change path: the
provider is still checked, a warning is logged once per error kind, and the run
is marked degraded. A skip is never derived from an errored lookup.

### Target prioritization (`prioritization`)

The registry answers not just *what* was found but *what to scan next*. Each
repository gets a deterministic, explainable priority from evidence already in
the registry — verified key status, push freshness and size — denormalized into
every `links.priority` row of that repository. This is a purely read-side
capability plus one reserved column write: pipeline decisions are untouched, no
network activity occurs and no shard format changes.

**Formula (defaults).**

```
priority = W1·[valid key] + W2·[wait_check/no_quota key]
           + W4·exp(−ln2·age_days / half_life_days)
           − W5·clamp((size_kb − threshold_kb) / (ramp_kb − threshold_kb), 0, 1)
```

| Weight | Default | Meaning |
|--------|---------|---------|
| `w1` | 100 | Repository has at least one attributed `valid` key (0 disables). |
| `w2` | 40 | Repository has only soft evidence (`wait_check`/`no_quota`). |
| `w4` | 30 | Peak freshness weight (decays with age). |
| `half_life_days` | 30 | Freshness half-life in days. |
| `w5` | 20 | Maximum size penalty. |
| `threshold_kb` | 50000 | Size below which no penalty applies (~50 MB). |
| `ramp_kb` | 500000 | Size at/above which the penalty is capped (~500 MB). |
| `display_top_n` | 0 | Optional StatusManager top-N candidates line (0 = off). |

Missing inputs degrade neutrally: `NULL` dates/sizes and absent keys contribute
zero and a fully sparse repository scores `0` without error. A repository's
`valid` key is attributed through `keys.source_url_hash → links.url_hash →
(owner, repo)`; legacy keys imported without a source URL contribute only to
global statistics, never to a repo score (documented limitation).

**Recomputation.** Key-status upserts, date/size merges and repo-wide metadata
merges mark the owning repository dirty; the next writer batch rescores it, and
a full sweep at run finish reconciles everything — scores never lag the ledger
by more than one run. All knobs are validated (weights non-negative, half-life
positive, `ramp_kb > threshold_kb`).

```yaml
registry:
  enabled: true
prioritization:
  w1: 100
  w2: 40
  w4: 30
  half_life_days: 30
  w5: 20
  threshold_kb: 50000
  ramp_kb: 500000
  display_top_n: 0     # cosmetic status line; off by default
```

**Candidate export (`tools/export_candidates.py`).** A read-only CLI publishes
the ordered corpus as the stable, schema-versioned handoff contract for the
future clone/TruffleHog project. NDJSON is the default; `--csv` is supported.
Records are sorted by `priority` desc, `repo_pushed_at` desc (nulls last), then
`owner`/`repo`, and carry `schema_version`, owner/repo, priority, best key
status, status counts, freshness/size, `links_total` and a bounded sample of
link URLs.

```bash
# All candidates, NDJSON to stdout
python -m tools.export_candidates --workspace ./data

# Top 100 with priority >= 50, CSV, up to 10 sample links each
python -m tools.export_candidates --workspace ./data \
    --csv --min-priority 50 --limit 100 --sample-links 10
```

Because every raw input is in the record, consumers can re-rank offline with
their own weights without touching the database. See
`docs/specs/candidates_export.md` for the frozen field dictionary, the
`schema_version` policy and advanced direct-SQLite guidance.

### Search-response aggregation (`aggregation`)

Providers whose conditions produce the identical wire query on the same
transport used to each run their own full GitHub search chain — waste that
refine expands deterministically (one broad web condition can expand into
hundreds of identical subquery chains per provider). A GitHub search response is
a pure function of `(transport, wire_query, page)` and is provider-agnostic
(patterns are applied only after the answer arrives), so within a short TTL
window one response can feed every provider.

**Layout.** Sharing lives strictly below the task boundary: the two
stage-facing dispatchers (`search_with_count`, `search_code`) route through a
TTL + byte-capped LRU store with singleflight coalescing. Every provider still
executes its own task and writes its own basket, shards and registry rows —
only the HTTP fetch is shared. The wire-query fingerprint
(`sha256("<api|web>|<wire_query>")`) is produced by one helper
(`search/querykey.py`) used by both task planning and the runtime cache key, so
the two cannot drift apart.

**Modes.** `aggregation.mode` is a three-position flag; the default `off` is
byte-for-byte the pre-change behavior.

- `off` — no sharing, no store maintenance. Kill-switch / rollback state.
- `shadow` — every fetch performs its real request; the store is maintained in
  parallel and each would-be hit is compared against the live answer (Jaccard
  of URL sets, total-count delta) and appended to
  `<workspace>/aggregation_decisions.jsonl`. Served results are always live.
- `on` — full sharing: a fresh hit is served without touching the transport
  (and therefore without consuming a rate-limit bucket, adaptive reporting or
  credential cooldown state); concurrent identical fetches coalesce onto one
  leader request.

```yaml
aggregation:
  mode: "off"          # off | shadow | on
  ttl_web_s: 120       # web response freshness window (1..3600)
  ttl_api_s: 300       # API response freshness window (1..3600)
  max_bytes: 67108864  # in-memory LRU cap (>= 1 MiB)
  join_timeout_s: 60   # singleflight joiner bound (1..600)
```

**TTL basis.** A live drift experiment (28 fetches, 2026-09-20) measured
Jaccard = 1.000 for identical queries across 900 s on both transports; the
defaults (web 120 s, API 300 s) are deliberately conservative against that
horizon and are raised only on shadow data.

**Safety.** Only fully successful returns are storable: typed transient
failures and limiter-suppressed blanks propagate verbatim to every waiter and
are never stored (the archived `fix-silent-losses` taxonomy raises before the
store is reached), while legitimate HTTP 200 zero-result answers are storable
and servable. Consumers receive independent URL-list and metadata copies;
immutable content is shared. Waiting on an in-flight leader is bounded by
`join_timeout_s`, after which the waiter issues its own request — the worst
case equals pre-change behavior and no task is ever dropped.

**Metrics.** Exposed in `PipelineStatus.aggregation_metrics`: `hits`, `misses`,
`joins`, `join_timeouts`, `evictions`, `entries`, `bytes`, `poisoned_rejected`
(should stay 0), `shadow_comparisons`, `shadow_jaccard_min` /
`shadow_jaccard_p95`, and the planning-time `aggregatable_pairs`. The store is
in-memory only: a restart starts cold and no workspace artifact is created in
`off`/`on` (the shadow decision log is the sole on-disk output, append-only
audit data).

**Promotion & rollback.** Run `shadow` for a representative production cycle
(including a rate-limit storm), review `aggregation_decisions.jsonl` (p95
Jaccard ≥ 0.95, deep-page divergence, hit-rate) and `aggregation_metrics`, then
flip `mode: on`. Rollback is the reverse config flip — no code change and no
data migration.

> **Sequencing.** Land the search-syntax-aware `clean_regex` fix
> (`fix-refine-query-split`) *before* promoting `mode` beyond `shadow`, so
> hit-rate and `aggregatable_pairs` statistics reflect live queries rather than
> cached zeros of dead conditions. That fix has no flag and no data surface —
> its rollback is simply reverting the commit, and it never changes
> `aggregation_decisions.jsonl` Jaccards for the already-correct query class.

### Search-work fan-out governor (`refine_governor`)

When a search's reported `total_count` exceeds the transport limit, the refine
engine splits the query and the search stage used to materialize **every**
resulting child, with no depth counter, no per-refine partition cap and no run
budget — and each child whose own total still exceeded the limit refined again.
On a broad seed this recursed without bound. The 2026-09-21 production incident
(systemd host, 4 API providers sharing `/sk-[a-zA-Z0-9]{32}/`, total ≈ 46 M)
reached a 3 999 992 / 4 000 000 search queue with ≈ 95 tasks processed,
**6.9 GB RSS + 7.4 GB swap**, OOM-killed after ~4 minutes, with thousands of
tasks lost to the persistence drain race. Historically the blow-up was masked by
silent `queue.Full` truncation at the default 100 000-slot queue; the incident
config raised the queue to 4 M and removed that brake.

The governor converts the de-facto ceiling into a **declared, deterministic,
observable, config-tunable** policy. It sits strictly between "the engine
produced candidate children" and "children enter the queue": it never rewrites
wire queries, fingerprints, baskets, dedup ids, early-stop or the failure
contract.

```yaml
refine_governor:
  mode: "on"                    # off | shadow | on
  max_refine_depth: 2           # refinement recursion cap (1..5)
  max_partitions_per_refine: 128  # per-refine partition clamp (positive)
  max_search_tasks_per_run: 10000 # admitted refined children per process run (> 0)
```

**Modes.** A three-position flag; flips require a restart.

- `off` — byte-equivalent pre-change behavior: unclamped partitions, no depth
  stop, budget inert, all counters silent. Rollback state.
- `shadow` — every child is enqueued exactly as in `off`, while every would-be
  clamp/truncation/depth/budget refusal is computed, counted and logged
  (WARNING). Measures the coverage price before enforcement.
- `on` — decisions enforced. **Default `on`** is a deliberate deviation from
  shadow-first shipping: the ungoverned state is an active, recorded OOM defect,
  and the historical de-facto ceiling was already stricter than these caps for
  any broad seed. Operators wanting measurement first flip to `shadow` in one
  line.

**What is enforced.**

- *Depth:* roots are depth 0; children get `parent + 1`; page tasks keep the
  parent's depth. At `max_refine_depth` a task that still exceeds the limit does
  **not** refine again — it falls through to ordinary pagination within the
  existing transport page cap (`API_MAX_PAGES`/`WEB_MAX_PAGES`), and
  `parents_at_depth_cap_paginated` increments.
- *Width:* the partition count handed to the generator is clamped to
  `max_partitions_per_refine`, so the uncapped candidate list is never
  materialized; the returned list is then sorted by ascending
  `search/querykey.fingerprint` (sha256 of `"<api|web>|<wire query>"`) and
  truncated to the cap. Sorting makes admission reproducible across processes
  regardless of the engine's set-based ordering. Fingerprint order — rather than
  lexicographic — also spreads the truncation gaps pseudo-randomly over the key
  space, so no fixed region is systematically excluded run after run: measured on
  the incident seed at cap 128 (144 candidates), lexicographic order dropped
  **every** `w x y z` child on every run (~11 % permanent blind spot) while
  fingerprint order drops a scattered few that differ per parent.
- *Volume:* refined children are admitted in that same fingerprint order until
  `max_search_tasks_per_run` is exhausted; the remainder is refused with a
  per-child WARNING naming provider, parent query and reason (`budget`). Root
  tasks (configured conditions) and page tasks are **exempt** — the configured
  conditions always run. The budget resets each process run.

**Default basis (2026-09-21 measurements).** The offline generator emits 72 /
1 296 / **46 656** children for partitions 64 / 1 000 / 46 007 — the last in
0.13 s (4 providers ⇒ 186 624 first-level tasks instantly, before recursion). A
live `api.github.com/search/code` calibration shows heavy skew: `"sk-000"` →
75 136 (refines again), `"sk-i00"` → 24, `"sk-z00"` → 9. Covering the 47.4 M root
through a 1 000-per-query API needs ≥ 47 449 fetches ≈ **88 h per provider** at
0.15 req/s, so full enumeration is economically impossible — coverage must be
traded loudly, not silently. `max_partitions_per_refine=128` ≈ 15 min of fetches
per parent and absorbs the engine's ~1.35× partition overshoot;
`max_search_tasks_per_run=10 000` ≈ 18.5 h ≈ one daily cycle at the configured
rate, ~10× below the old silent-truncation queue ceiling.

**Coverage trade-off.** Broad seeds are covered **partially by design**. The
coverage estimate per refined parent is `min(1.0, admitted × limit / max(total,
1))` — the fraction of the reported space reachable if every admitted child fills
a page window. It ignores child-total skew and is an approximation; `shadow` mode
calibrates expectations before relying on `on`. Raising the caps is a config
edit; lowering is a one-flip rollback.

**Metrics.** Exposed in `PipelineStatus.refine_metrics`: `mode`,
`children_generated`, `children_admitted`, `refused_depth`, `refused_budget`,
`truncated_to_cap`, `parents_refined`, `parents_at_depth_cap_paginated`,
`budget_remaining`, `coverage_estimate_min` / `coverage_estimate_avg`. Each
refined parent also logs an INFO line (`generated` / `admitted` / `truncated` /
`refused_budget` / `coverage`).

**Relationships.** The env seatbelts `REGEX_MAX_QUERIES` / `REGEX_MAX_DEPTH`
(`search/github/refine/config.py`) remain as engine-level defense in depth but are
**superseded as policy** by this section. This change is step R1 of the
remediation roadmap (`plan.md`): it is the safety precondition for R2
(`fix-queue-persistence-under-load`) — durable-queue backpressure is only safe
with bounded inflow — and a force multiplier for R3.

> **Operations: `queue_sizes.search`.** With the governor bounding inflow (budget
> ≪ queue), the incident-sized `queue_sizes.search: 4000000` no longer serves a
> purpose and MAY be lowered back to a sane value (e.g. `100000`). Do this **only
> together with R2** (`fix-queue-persistence-under-load`): until the
> put-on-`Full` semantics are fixed, a smaller queue still risks the historical
> silent-drop path under any unforeseen inflow. Rollback of the governor itself
> is `mode: off`.

### Failure handling (`pipeline.failure_handling`)

Transient fetch failures used to be indistinguishable from legitimate empty
results: a network error after retries, a limiter suppression or a blank payload
collapsed into an empty search page (counted as a processed task and deduplicated
away for the rest of the run) or into a `gathered_ok` registry row (suppressing
re-gather until TTL expiry). `pipeline.failure_handling` governs the fix, which
classifies every empty outcome as either a **legitimate zero** (the remote
answered successfully with zero matches) or a **failure-empty** (no usable answer
was obtained) and routes the latter into the existing bounded-requeue machinery.

```yaml
pipeline:
  failure_handling: "shadow"   # legacy | shadow | strict
```

**Modes**

- `legacy` — byte-for-byte pre-change behavior: typed failures are swallowed and
  the new counters stay inert. The pure rollback / kill-switch state.
- `shadow` (default at release) — every failure-empty is detected, counted in
  `failure_empties_detected` and logged with stage/provider/task context, while
  task outcomes and harvest output remain exactly as in `legacy`. This measures
  real-world frequency without changing behavior.
- `strict` — full enforcement: a failed task is re-enqueued with its attempt
  counter incremented and counted as an error (never as a success); a gather
  whose blob fetch failed is recorded `visit_status='failed'` with no coverage
  row, making the existing `regathered_failed_retry` path reachable; a
  provider-limiter-starved check requeues instead of vanishing. After
  `global.max_retries_requeued` attempts the task is dropped loudly (warning log
  + counter).

**Counters** (per stage, in `StageMetrics` and the status surface):
`failure_empties_detected`, `tasks_requeued`, `tasks_dropped_max_retries`.
`failure_empties_detected` operates in `shadow` and `strict`; the requeue/drop
counters tick only when `strict` actually propagates a failure. Search failures
also now increment `total_errors` (they previously did not).

**Config lint.** The validator emits an advisory warning (never an error) when a
`use_api: true` provider carries a web-only qualifier such as `content:` in a
condition query, because the code-search REST API silently returns zero matches
for it (live probe 2026-09-20). The qualifier list is data-driven
(`constant/search.py`, `WEB_ONLY_QUALIFIERS`), so extending it needs no validator
change.

**Promotion & rollback (project paradigm).** Ship `shadow`, run one
representative cycle (including a rate-limit storm), review
`failure_empties_detected` per stage and the dead-qualifier warnings, then
promote to `strict` with a config flip and watch `tasks_requeued` /
`tasks_dropped_max_retries` and registry `visit_status='failed'` inflow. Rollback
is a config flip back to `legacy` — no code removal and no data migration;
historical false `gathered_ok` rows age out via `skip.gather_ttl_hours`.

**Unchanged:** credential cooldown/rotation (60→900s escalation, blocking
`_get_available`), NDJSON shard formats, `recover_tasks()` replay, registry
schema/SQL, dedup-id structure and wire-query construction. Parsing of a
successfully fetched payload stays fail-open: malformed data degrades to
empty/NULL with a counted warning, never an exception.

## Troubleshooting

### **Common Issues**

#### **1. Installation Problems**
```bash
# Issue: pip install fails
# Solution: Upgrade pip and use virtual environment
python -m pip install --upgrade pip
python -m venv venv

# Linux/macOS
source venv/bin/activate

# Windows
venv\Scripts\activate

pip install -r requirements.txt
```

#### **2. Configuration Errors**
```bash
# Issue: Configuration validation fails
# Solution: Validate configuration file
python main.py --validate

# Issue: Missing configuration file
# Solution: Create from example
cp examples/config-simple.yaml config.yaml
```

#### **3. Rate Limiting Issues**
```bash
# Issue: Too many API requests
# Solution: Adjust rate limits in config
rate_limits:
  github_api:
    base_rate: 0.1  # Reduce rate
    adaptive: true  # Enable adaptive limiting
```

#### **4. Memory Issues**
```bash
# Issue: High memory usage
# Solution: Reduce batch sizes and thread counts
pipeline:
  threads:
    search: 1
    gather: 2  # Reduce from default
persistence:
  batch_size: 25  # Reduce from default 50
```

#### **5. Network Connectivity**
```bash
# Issue: Connection timeouts
# Solution: Increase timeout values
api:
  timeout: 60  # Increase from default 30
  retries: 5   # Increase retry attempts
```

### **Debug Mode**
```bash
# Enable debug logging
python main.py --log-level DEBUG

# Save debug output to file
python main.py --log-level DEBUG > debug.log 2>&1
```

## Security Considerations

### **Credential Management**
- **Never commit credentials** to version control
- **Use environment variables** for sensitive configuration
- **Rotate credentials regularly** to minimize exposure risk
- **Implement least privilege** access for API keys

### **Data Protection**
```yaml
# Example: Secure credential configuration
global:
  github_credentials:
    sessions:
      - "${GITHUB_SESSION_1}"  # Use environment variables
      - "${GITHUB_SESSION_2}"
    tokens:
      - "${GITHUB_TOKEN_1}"
```

### **Privacy Considerations**
- **Respect robots.txt** and website terms of service
- **Implement rate limiting** to avoid overwhelming target services
- **Log redaction** automatically removes sensitive data from logs
- **Data retention policies** should comply with applicable regulations

### **Compliance Guidelines**
- **Review legal requirements** before using in production
- **Obtain necessary permissions** for data collection
- **Implement data anonymization** where required
- **Document data processing** activities for compliance

## Important Notes

1. **Limitations**
   - Respect Github API usage limits
   - Configure rate limits appropriately
   - Mind memory usage
   - Handle sensitive data carefully

2. **Best Practices**
   - Use appropriate thread counts
   - Backup results regularly
   - Monitor error rates
   - Handle alerts promptly

## TODO & Roadmap

### 🏗️ Core Architecture Improvements

#### Data Source Abstraction
- [ ] **Abstract Data Source Interface**: Create a unified interface for all data sources
  - [ ] Define `DataSourceProvider` base class with standard methods (`search`, `gather`, `validate`)
  - [ ] Implement adapter pattern for different API formats (REST, GraphQL, WebSocket)
  - [ ] Add configuration schema for data source registration
  - [ ] Support dynamic data source loading and hot-swapping

#### Stage System Enhancement
- [ ] **Flexible Stage Definition**: Move beyond the current 4-stage limitation
  - [ ] Create `StageDefinition` configuration format (YAML/JSON)
  - [ ] Implement dynamic stage loading from configuration files
  - [ ] Add stage composition and conditional execution
  - [ ] Support user-defined stage workflows and DAG customization

#### Handler/Processor Registration System
- [ ] **Pluggable Processing Architecture**: Replace fixed function calls with configurable handlers
  - [ ] Implement `HandlerRegistry` for stage-specific processors
  - [ ] Create `ProcessorInterface` with standardized input/output contracts
  - [ ] Add handler discovery mechanism (annotation-based or configuration-driven)
  - [ ] Support middleware chains for request/response processing

### 🌐 Data Source Integrations

#### Network Mapping Platforms
- [ ] **FOFA Integration**
  - [ ] Implement FOFA API client with authentication
  - [ ] Add FOFA-specific query optimization

- [ ] **Shodan Integration**
  - [ ] Support data querying and extraction from Shodan

#### Generic Web Sources
- [ ] **Universal Web Scraper**
  - [ ] Build configurable web scraping engine
  - [ ] Add support for JavaScript-rendered content (Selenium/Playwright)
  - [ ] Implement anti-bot detection bypass mechanisms
  - [ ] Create content extraction rule engine

### 🔧 Framework Enhancements

#### Configuration & Extensibility
- [ ] **Plugin System**
  - [ ] Design plugin architecture with lifecycle management
  - [ ] Create plugin marketplace and discovery mechanism
  - [ ] Add plugin sandboxing and security validation
  - [ ] Implement plugin dependency resolution

#### Performance & Scalability
- [ ] **Distributed Processing**
  - [ ] Add support for distributed task execution (Celery/RQ)
  - [ ] Implement horizontal scaling with load balancing
  - [ ] Create cluster management and node discovery
  - [ ] Add distributed state synchronization

#### Security
- [ ] **Enhanced Security Features**
  - [ ] Implement credential encryption and secure storage
  - [ ] Create rate limiting policies per data source

### 📊 Monitoring & Analytics

#### Advanced Monitoring
- [ ] **Real-time Analytics Dashboard**
  - [ ] Build web-based monitoring interface
  - [ ] Add real-time metrics visualization
  - [ ] Implement alerting and notification system
  - [ ] Create performance profiling and bottleneck analysis



### 🚀 Advanced Features

#### API & Integration
- [ ] **RESTful API Server**
  - [ ] Build comprehensive REST API for external integration
  - [ ] Implement webhook support for real-time notifications
  - [ ] Create SDK libraries for popular programming languages

## Contributing

Contributions are welcome! Before submitting a pull request, please ensure:

1. Tests are updated
2. Code follows style guidelines
3. Documentation is added where necessary
4. All tests pass

### Priority Areas for Contributors

- 🔥 **High Priority**: Data source abstraction and FOFA/Shodan integration
- 🔥 **High Priority**: Stage system flexibility and handler registration
- 🔥 **High Priority**: Plugin architecture and extensibility framework
- 🔥 **Medium Priority**: Performance optimization and distributed processing
- 🔥 **Medium Priority**: Web-based monitoring dashboard

## License

This project is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0). See the [LICENSE](LICENSE) file for details.

## Disclaimer

**⚠️ IMPORTANT NOTICE**

This project is developed **solely for educational and technical research purposes**. Users should exercise caution and responsibility when using this software.

**Key Points:**
- This software is intended for learning, research, and educational use only
- Users must comply with all applicable laws and regulations in their jurisdiction
- Users are responsible for ensuring their usage complies with the terms of service of any third-party platforms or APIs
- **The project authors do not recommend, encourage, or endorse the use of this software for illegally obtaining others' API keys or credentials**
- The project authors assume **no responsibility** for any disputes, legal issues, or damages arising from the use of this software
- Commercial use is strictly prohibited without explicit written permission
- Users should respect the intellectual property rights and privacy of others

**By using this software, you acknowledge that you have read, understood, and agree to these terms. Use at your own risk.**



## Contact

For questions or other inquiries during usage, please contact the project maintainers through GitHub Issues.