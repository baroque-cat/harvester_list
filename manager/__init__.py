#!/usr/bin/env python3

"""
Manager Package - Core Management Components

This package provides core management components for the application:
- Resource management (credentials, agents, authentication)
- Task and pipeline management
- Queue management and coordination
- Worker management and load balancing

For storage and persistence operations, use the 'storage' package.
"""

# Pipeline and task management
from .pipeline import Pipeline

# Queue management
from .queue import QueueConfig, QueueManager, QueueStateInfo
from .task import TaskManager, create_task_manager

# Worker management
from .worker import WorkerManager, create_worker_manager

__all__ = [
    # Pipeline and task management
    "Pipeline",
    "TaskManager",
    "create_task_manager",
    # Queue management
    "QueueConfig",
    "QueueManager",
    "QueueStateInfo",
    # Worker management
    "WorkerManager",
    "create_worker_manager",
]
