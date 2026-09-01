import { SkeletonStatTiles } from "@/components/ui/Skeleton";

export default function Loading() {
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold text-foreground">Analytics</h1>
      <SkeletonStatTiles />
    </div>
  );
}
