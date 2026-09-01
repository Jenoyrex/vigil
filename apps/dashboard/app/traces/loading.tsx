import { SkeletonTable } from "@/components/ui/Skeleton";

export default function Loading() {
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold text-foreground">Traces</h1>
      <SkeletonTable rows={10} />
    </div>
  );
}
