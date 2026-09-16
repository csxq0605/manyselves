import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import type { Project, ProjectApi } from "./project-api";

interface ActivationAttempt {
  readonly key: string;
  readonly promise: Promise<Project>;
}

export function useProjectActivation(
  projectId: string | undefined,
  routeProject: Project | undefined,
  projectApi: ProjectApi,
) {
  const queryClient = useQueryClient();
  const attemptRef = useRef<ActivationAttempt | null>(null);
  const [activationErrorKey, setActivationErrorKey] = useState<string | null>(null);
  const activationKey = projectId && routeProject && !routeProject.active ? projectId : null;

  useEffect(() => {
    if (!activationKey) return;

    let attempt = attemptRef.current;
    if (!attempt || attempt.key !== activationKey) {
      const promise = projectApi.activate(activationKey).then((activated) => {
        queryClient.setQueryData<Project[]>(["projects"], (current) => current?.map((project) => (
          project.id === activated.id ? activated : { ...project, active: false }
        )));
        queryClient.removeQueries({ queryKey: ["conversations"] });
        queryClient.removeQueries({ queryKey: ["conversation-messages"] });
        return activated;
      });
      attempt = { key: activationKey, promise };
      attemptRef.current = attempt;
      void promise.then(
        () => { if (attemptRef.current === attempt) attemptRef.current = null; },
        () => { if (attemptRef.current === attempt) attemptRef.current = null; },
      );
    }

    let cancelled = false;
    void attempt.promise.then(
      () => {
        if (!cancelled) setActivationErrorKey((current) => current === activationKey ? null : current);
      },
      () => { if (!cancelled) setActivationErrorKey(activationKey); },
    );
    return () => { cancelled = true; };
  }, [activationKey, projectApi, queryClient]);

  return {
    isError: Boolean(activationKey && activationErrorKey === activationKey),
    isReady: Boolean(routeProject?.active),
  };
}
