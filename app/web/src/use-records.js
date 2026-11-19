import { useApp } from "./ui";
import { useQuery } from "./query";
export function useRecords(path) {
  const { data } = useApp();
  const query = useQuery(path, data.revision);
  // Third element is additive: existing `[result, error]` callers stay compatible.
  return [query.result, query.error, query.reload];
}
