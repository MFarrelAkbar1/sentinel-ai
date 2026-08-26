"""The four agents and the LangGraph orchestrator that coordinates them.

Agent 1 — FinancialAuditor          extraction + deterministic verification
Agent 2 — MarketIntelligenceAnalyst weighted market context (parallel with 1)
Agent 3 — RedFlagInvestigator       hypotheses + active falsification
Agent 4 — Synthesizer               tiered report, confidence, suppression
"""

from agents.financial_auditor import FinancialAuditor, financial_auditor_node
from agents.market_intelligence import MarketIntelligenceAnalyst, market_intelligence_node
from agents.red_flag_investigator import RedFlagInvestigator, red_flag_investigator_node
from agents.synthesizer import Synthesizer, synthesizer_node

__all__ = [
    "FinancialAuditor",
    "MarketIntelligenceAnalyst",
    "RedFlagInvestigator",
    "Synthesizer",
    "financial_auditor_node",
    "market_intelligence_node",
    "red_flag_investigator_node",
    "synthesizer_node",
]
