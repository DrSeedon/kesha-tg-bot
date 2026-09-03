# Worker memory

- Verify lifecycle fallbacks at the terminal resource, not at an intermediate
  call counter: `reconnect()` can defer cleanup to a future owner that no longer
  exists. A bounded caller should stop waiting without cancelling the owned
  terminate/kill task.
