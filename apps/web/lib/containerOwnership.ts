import type { ActorSummary, ContainerSummary } from "./mockData";

export function ownsContainer(
  container: ContainerSummary,
  actor: Pick<ActorSummary, "memberId" | "userId">,
): boolean {
  return container.ownerMemberId
    ? container.ownerMemberId === actor.memberId
    : container.user === actor.userId;
}
