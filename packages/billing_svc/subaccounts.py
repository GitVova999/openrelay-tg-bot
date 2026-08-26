"""Custodial subaccount derivation (Simple tier).

Master seed → per-channel subaccount via BIP-32-style derivation.  Address is
deterministic from `channel.id`, so we can regenerate keys on demand rather
than persisting them.  Regeneration requires the master seed → same trust
boundary as the master; on-disk we keep only the derivation_index.

Not yet implemented — stub carries the interface contract.
"""

def derive_evm_address(channel_id: int) -> str:
    """TODO: HKDF(master_seed, "evm/base/" + channel_id) → privkey → address."""
    raise NotImplementedError


def derive_solana_address(channel_id: int) -> str:
    """TODO: HKDF(master_seed, "solana/" + channel_id) → ed25519 kp → pubkey."""
    raise NotImplementedError
