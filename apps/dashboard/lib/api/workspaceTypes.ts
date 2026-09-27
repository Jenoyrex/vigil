/** Mirrors apps/api/app/schemas/workspace.py. Safe to import from client code (types only). */

export interface Project {
  id: string;
  name: string;
  slug: string;
  created_at: string;
}

export interface Organization {
  id: string;
  name: string;
  slug: string;
  role: "owner" | "admin" | "member";
  projects: Project[];
}

export interface Me {
  user: { id: string; email: string; full_name: string | null };
  organizations: Organization[];
}

export interface ApiKeyOut {
  id: string;
  name: string;
  key_prefix: string;
  status: "active" | "revoked";
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface ApiKeyList {
  items: ApiKeyOut[];
}

/** `api_key` is the raw key, returned once at creation and never again. */
export interface ApiKeyCreated extends ApiKeyOut {
  api_key: string;
}
