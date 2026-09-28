"""Deterministic Economic Machine reference kernel, independent of data feeds."""

from .compiler import compile_program
from .basket import commit_basket, verify_basket
from .authenticated_basket import commit_authenticated_basket, verify_authenticated_basket
from .chain_binding import (prepare_chain_binding, verify_chain_binding,
                            prepare_authenticated_chain_binding,
                            compute_attestation_digest, prepare_attestation_message,
                            verify_authenticated_chain_binding)
from .grid_search import search_grid, verify_grid
from .kernel import EconomicKernel
from .inference import assess_inference
from .portfolio import select_portfolio
from .product_adapter import assemble_portfolio_inputs, verify_portfolio_inputs
from .signed_evidence import assemble_signed_portfolio_inputs, verify_signed_portfolio_inputs
from .runtime import MachineRuntime
from .state import apply_delta
from .tron_registry_read import assess_registry_observation, read_registry_observation
from .tron_consumption_read import assess_basket_consumption, read_tron_transaction

__all__ = ["compile_program", "EconomicKernel", "MachineRuntime", "apply_delta",
           "assess_inference", "select_portfolio", "assemble_portfolio_inputs",
           "verify_portfolio_inputs", "assemble_signed_portfolio_inputs",
           "verify_signed_portfolio_inputs", "commit_basket", "verify_basket",
           "commit_authenticated_basket", "verify_authenticated_basket",
           "prepare_authenticated_chain_binding", "verify_authenticated_chain_binding",
           "compute_attestation_digest", "prepare_attestation_message",
           "search_grid", "verify_grid", "prepare_chain_binding",
           "verify_chain_binding", "read_registry_observation",
           "assess_registry_observation", "read_tron_transaction",
           "assess_basket_consumption"]
