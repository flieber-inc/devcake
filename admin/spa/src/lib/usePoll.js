import { useEffect } from "react";

// Run fn immediately and every `ms` after; re-arms when deps change.
export default function usePoll(fn, ms, deps = []) {
  useEffect(() => {
    fn();
    const t = setInterval(fn, ms);
    return () => clearInterval(t);
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
}
