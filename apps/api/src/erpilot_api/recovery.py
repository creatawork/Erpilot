"""API composition for the ERP mutation reconciliation adapter."""

from mcp_erp.bridge import build_mutation_reconciler


def erp_mutation_reconciler(db_path=None):
    """Build a read-only reconciler against the configured ERP database."""
    return build_mutation_reconciler() if db_path is None else build_mutation_reconciler(db_path)
