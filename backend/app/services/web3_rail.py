class Web3Rail:

  def __init__(self):
    self.is_connected = True

  def log_match_result(self, match_id: str, winner_phone: str):
    print(
        f"[Web3 Rail] Immutable log added for Match {match_id} -> Winner:"
        f" {winner_phone}"
    )
    return True
