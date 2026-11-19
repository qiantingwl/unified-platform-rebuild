import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { createRecordLoader } from "./record-loader";
export function queryString(values) {
  return new URLSearchParams(
    Object.entries(values).filter(
      ([, v]) => v !== "" && v !== null && v !== undefined && v !== "all",
    ),
  );
}
export function useDebounced(value, delay = 250) {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return settled;
}
// New filters cancel obsolete requests. Revisions coalesce behind a slow active read.
export function useQuery(
  path,
  revision = 0,
  generation = 0,
  { retainPrevious = false } = {},
) {
  const [state, setState] = useState({
    path: null,
    result: null,
    resultPath: null,
    error: "",
    loading: !!path,
  });
  const loader = useRef(null);
  const canRetain = (old) =>
    Boolean(
      path &&
      old.resultPath &&
      (old.resultPath === path ||
        (retainPrevious &&
          old.resultPath.split("?")[0] === path.split("?")[0])),
    );
  useEffect(() => {
    if (!path) {
      setState({
        path: null,
        result: null,
        resultPath: null,
        error: "",
        loading: false,
      });
      return;
    }
    setState((old) => ({
      path,
      result: canRetain(old) ? old.result : null,
      resultPath: canRetain(old) ? old.resultPath : null,
      generation: canRetain(old) ? old.generation : undefined,
      activeGeneration: generation,
      error: "",
      loading: true,
    }));
    const current = createRecordLoader({
      fetchRecords: (signal) => {
        setState((old) => ({ ...old, loading: true, error: "" }));
        return api(path, "GET", undefined, { signal });
      },
      onSuccess: (result) =>
        setState({
          path,
          result,
          resultPath: path,
          generation,
          activeGeneration: generation,
          error: "",
          loading: false,
        }),
      onError: (error) =>
        setState((old) => ({
          ...old,
          path,
          error:
            error.name === "AbortError" ? "查询超时，请重试。" : error.message,
          loading: false,
        })),
    });
    loader.current = current;
    return () => {
      current.stop();
      if (loader.current === current) loader.current = null;
    };
  }, [path, generation, retainPrevious]);
  useEffect(() => {
    loader.current?.refresh();
  }, [path, revision, generation, retainPrevious]);
  const visible =
    state.path === path && state.activeGeneration === generation
      ? state
      : {
          result: canRetain(state) ? state.result : null,
          resultPath: canRetain(state) ? state.resultPath : null,
          generation: canRetain(state) ? state.generation : undefined,
          error: "",
          loading: !!path,
        };
  return {
    ...visible,
    isPrevious: Boolean(
      visible.resultPath &&
      (visible.resultPath !== path || visible.generation !== generation),
    ),
    reload: () => loader.current?.refresh(),
  };
}
