import { useEffect, useMemo, useState } from "react";
import { Eye, EyeOff, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import type { AgentView, JsonSchema } from "@/types";

interface AgentStartDialogProps {
  agent: AgentView | null;
  open: boolean;
  submitting?: boolean;
  onOpenChange: (open: boolean) => void;
  onStart: (inputs: Record<string, unknown>) => Promise<void>;
}

type FormValues = Record<string, unknown>;

function schemaType(schema: JsonSchema): JsonSchema["type"] {
  return Array.isArray(schema.type) ? schema.type.find((type) => type !== "null") : schema.type;
}

function isNullSchema(schema: JsonSchema): boolean {
  return schemaType(schema) === "null" || (schema.enum?.length ? schema.enum.every((value) => value === null) : false);
}

/** Resolve Pydantic v2's local definitions and simplify nullable unions. */
function resolveSchema(input: JsonSchema, root: JsonSchema, depth = 0): JsonSchema {
  if (depth > 8) return input;
  let current = input;
  const seenRefs = new Set<string>();
  while (current.$ref && current.$ref.startsWith("#/$defs/")) {
    if (seenRefs.has(current.$ref)) break;
    seenRefs.add(current.$ref);
    const definition = root.$defs?.[current.$ref.slice("#/$defs/".length)];
    if (!definition) break;
    const siblings = Object.fromEntries(Object.entries(current).filter(([key]) => key !== "$ref"));
    current = { ...definition, ...siblings };
  }

  const variants = current.anyOf ?? current.oneOf;
  if (variants?.length) {
    const resolvedVariants = variants.map((variant) => resolveSchema(variant, root, depth + 1));
    const nullable = resolvedVariants.some(isNullSchema);
    const nonNullVariants = resolvedVariants.filter((variant) => !isNullSchema(variant));
    const selected = nonNullVariants[0] ?? { type: "null" as const };
    const enumValues = nonNullVariants.flatMap((variant) => variant.enum ?? []);
    const withoutUnion = Object.fromEntries(Object.entries(current).filter(([key]) => key !== "anyOf" && key !== "oneOf"));
    current = {
      ...withoutUnion,
      ...selected,
      ...(nullable ? { nullable: true } : {}),
      ...(enumValues.length ? { enum: [...new Set(enumValues)] } : {}),
    };
  }

  const nullableType = Array.isArray(current.type) && current.type.includes("null");
  const enumIncludesNull = current.enum?.includes(null) ?? false;
  if (nullableType || enumIncludesNull) {
    const types = Array.isArray(current.type) ? current.type.filter((type) => type !== "null") : current.type;
    const enums = current.enum?.filter((value) => value !== null);
    current = {
      ...current,
      ...(types && types.length ? { type: types.length === 1 ? types[0] : types } : {}),
      ...(enums?.length ? { enum: enums } : { enum: undefined }),
      nullable: true,
    } as JsonSchema;
  }
  return current;
}

function defaultValue(schema: JsonSchema, required: boolean): unknown {
  if (schema.default !== undefined) return schema.default;
  if (schema.nullable) return required ? null : undefined;
  if (schemaType(schema) === "boolean") return required ? false : undefined;
  if (schemaType(schema) === "array") return required ? [] : undefined;
  if (schemaType(schema) === "object") return required ? {} : undefined;
  return "";
}

function initialValues(schema: JsonSchema, root: JsonSchema): FormValues {
  return Object.fromEntries(
    Object.entries(schema.properties ?? {}).map(([name, rawField]) => {
      const field = resolveSchema(rawField, root);
      return [name, defaultValue(field, schema.required?.includes(name) ?? false)];
    }),
  );
}

function isJsonEditor(schema: JsonSchema): boolean {
  return schemaType(schema) === "array" || schemaType(schema) === "object";
}

function isTextArea(schema: JsonSchema): boolean {
  return schema.format === "textarea" || schema["x-ui"] === "textarea" || (schema.maxLength ?? 0) > 240;
}

function displayName(name: string, schema: JsonSchema): string {
  return schema.title ?? name.replace(/[_-]+/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function jsonEditorValue(value: unknown, schema: JsonSchema): string {
  if (typeof value === "string") return value;
  if (value === undefined) return schemaType(schema) === "array" ? "[]" : "{}";
  return JSON.stringify(value, null, 2) ?? "";
}

function parseJsonValue(value: unknown): { value?: unknown; error?: string } {
  if (typeof value !== "string") return { value };
  try {
    return { value: JSON.parse(value) };
  } catch {
    return { error: "Value must be valid JSON." };
  }
}

function normalizeInputs(values: FormValues, schema: JsonSchema, root: JsonSchema): { inputs?: FormValues; errors: Record<string, string> } {
  const inputs: FormValues = {};
  const errors: Record<string, string> = {};
  const required = new Set(schema.required ?? []);

  for (const [name, rawField] of Object.entries(schema.properties ?? {})) {
    const field = resolveSchema(rawField, root);
    const raw = values[name];
    const blank = raw === undefined || (typeof raw === "string" && raw.trim() === "") || (raw === null && !field.nullable);
    if (required.has(name) && blank) {
      errors[name] = "This field is required.";
      continue;
    }
    if (blank && !required.has(name)) continue;
    if (raw === null && field.nullable) {
      if (required.has(name)) inputs[name] = null;
      continue;
    }

    let value = raw;
    if (isJsonEditor(field)) {
      const parsed = parseJsonValue(raw);
      if (parsed.error) {
        errors[name] = "Enter valid JSON.";
        continue;
      }
      value = parsed.value;
      if (schemaType(field) === "array" && !Array.isArray(value)) errors[name] = "Enter a JSON array.";
      if (schemaType(field) === "object" && (typeof value !== "object" || value === null || Array.isArray(value))) errors[name] = "Enter a JSON object.";
      if (!required.has(name) && ((Array.isArray(value) && value.length === 0) || (typeof value === "object" && value !== null && !Array.isArray(value) && Object.keys(value).length === 0)) && field.default === undefined) continue;
    } else if (schemaType(field) === "integer" || schemaType(field) === "number") {
      const numeric = Number(raw);
      if (!Number.isFinite(numeric) || (schemaType(field) === "integer" && !Number.isInteger(numeric))) {
        errors[name] = schemaType(field) === "integer" ? "Enter a whole number." : "Enter a number.";
        continue;
      }
      value = numeric;
    }
    if (field.enum && !field.enum.some((option) => Object.is(option, value))) {
      errors[name] = "Choose one of the available options.";
      continue;
    }
    if (!errors[name]) inputs[name] = value;
  }
  return { inputs: Object.keys(errors).length ? undefined : inputs, errors };
}

export function AgentStartDialog({ agent, open, submitting = false, onOpenChange, onStart }: AgentStartDialogProps) {
  const schema = agent?.input_schema;
  const resolvedSchema = useMemo(() => schema ? resolveSchema(schema, schema) : null, [schema]);
  const [values, setValues] = useState<FormValues>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [visibleSecrets, setVisibleSecrets] = useState<Record<string, boolean>>({});

  useEffect(() => {
    if (!open || !schema || !resolvedSchema) return;
    setValues(initialValues(resolvedSchema, schema));
    setErrors({});
    setSubmitError(null);
    setVisibleSecrets({});
  }, [open, schema, resolvedSchema]);

  const fields = useMemo(() => Object.entries(resolvedSchema?.properties ?? {}), [resolvedSchema]);
  if (!agent || !schema || !resolvedSchema) return null;

  const setValue = (name: string, value: unknown) => {
    setValues((current) => ({ ...current, [name]: value }));
    setErrors((current) => ({ ...current, [name]: "" }));
  };

  const submit = async () => {
    const result = normalizeInputs(values, resolvedSchema, schema);
    setErrors(result.errors);
    setSubmitError(null);
    if (!result.inputs) return;
    try {
      await onStart(result.inputs);
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[min(780px,calc(100vh-2rem))] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Start {agent.name}</DialogTitle>
          <DialogDescription>{agent.description || "Provide inputs for this agent."}</DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {fields.length === 0 && <p className="text-sm text-muted-foreground">This agent does not require any inputs.</p>}
          {fields.map(([name, field]) => {
            const resolvedField = resolveSchema(field, schema);
            const required = resolvedSchema.required?.includes(name) ?? false;
            const fieldType = schemaType(resolvedField);
            const secret = Boolean(resolvedField.writeOnly || resolvedField.secret || resolvedField.format === "password");
            const fieldError = errors[name];
            const label = displayName(name, resolvedField);
            return (
              <div key={name} className="space-y-1.5">
                <div className="flex items-center justify-between gap-3">
                  <Label htmlFor={`agent-${name}`}>
                    {label}{required && <span className="text-destructive"> *</span>}
                  </Label>
                  {fieldType === "boolean" && (
                    <Switch
                      id={`agent-${name}`}
                      checked={values[name] === true}
                      onCheckedChange={(checked) => setValue(name, checked)}
                      aria-invalid={Boolean(fieldError)}
                    />
                  )}
                </div>
                {resolvedField.description && <p className="text-xs text-muted-foreground">{resolvedField.description}</p>}
                {fieldType === "boolean" ? (
                  <span className="text-xs text-muted-foreground">{values[name] ? "Enabled" : "Disabled"}</span>
                ) : isJsonEditor(resolvedField) ? (
                  <Textarea
                    id={`agent-${name}`}
                    value={jsonEditorValue(values[name], resolvedField)}
                    onChange={(event) => setValue(name, event.target.value)}
                    className="min-h-24 font-mono text-xs"
                    aria-invalid={Boolean(fieldError)}
                    spellCheck={false}
                  />
                ) : secret ? (
                  <div className="relative">
                    <Input
                      id={`agent-${name}`}
                      type={visibleSecrets[name] ? "text" : "password"}
                      value={String(values[name] ?? "")}
                      onChange={(event) => setValue(name, event.target.value)}
                      aria-invalid={Boolean(fieldError)}
                      className="pr-10"
                    />
                    <button type="button" onClick={() => setVisibleSecrets((current) => ({ ...current, [name]: !current[name] }))} className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground" aria-label={visibleSecrets[name] ? "Hide value" : "Show value"}>
                      {visibleSecrets[name] ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                    </button>
                  </div>
                ) : resolvedField.enum ? (
                  <select
                    id={`agent-${name}`}
                    value={values[name] === undefined ? "" : JSON.stringify(values[name])}
                    onChange={(event) => setValue(name, event.target.value === "" ? undefined : JSON.parse(event.target.value) as unknown)}
                    aria-invalid={Boolean(fieldError)}
                    className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {!required && <option value="">Select an option</option>}
                    {resolvedField.nullable && <option value="null">None</option>}
                    {resolvedField.enum.map((option) => (
                      <option key={JSON.stringify(option)} value={JSON.stringify(option)}>{String(option)}</option>
                    ))}
                  </select>
                ) : isTextArea(resolvedField) ? (
                  <Textarea
                    id={`agent-${name}`}
                    value={String(values[name] ?? "")}
                    onChange={(event) => setValue(name, event.target.value)}
                    aria-invalid={Boolean(fieldError)}
                  />
                ) : (
                  <div className="relative">
                    <Input
                      id={`agent-${name}`}
                      type={fieldType === "number" || fieldType === "integer" ? "number" : "text"}
                      step={fieldType === "integer" ? "1" : fieldType === "number" ? "any" : undefined}
                      value={String(values[name] ?? "")}
                      onChange={(event) => setValue(name, event.target.value)}
                      aria-invalid={Boolean(fieldError)}
                      className={secret ? "pr-10" : undefined}
                    />
                  </div>
                )}
                {fieldError && <p className="text-xs text-destructive">{fieldError}</p>}
              </div>
            );
          })}
          {submitError && <p className="text-sm text-destructive">{submitError}</p>}
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>Cancel</Button>
          <Button type="button" onClick={() => void submit()} disabled={submitting}>
            {submitting && <Loader2 className="size-4 animate-spin" />}
            Start Agent
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
