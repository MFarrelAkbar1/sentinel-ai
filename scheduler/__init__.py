"""Autonomous trigger layer.

``SentinelMonitor`` is deliberately not re-exported here: ``python -m
scheduler.monitor`` imports the package first, and an eager re-export would
make the module appear in ``sys.modules`` before ``runpy`` executes it. Import
it from its module instead::

    from scheduler.monitor import SentinelMonitor
"""
