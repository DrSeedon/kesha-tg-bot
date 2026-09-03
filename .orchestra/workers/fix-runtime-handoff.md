# Worker memory

- For cancel-safe lifecycle transactions, test cancellation on both sides of
  the commit/adoption point. Run cleanup in a shielded task and still await it;
  fire-and-forget cleanup only moves the leak outside the caller.
