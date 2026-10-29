{{/* Names. Objects are named <fullname>[-<part>], all from the release. */}}
{{- define "tokenlean-lite.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "tokenlean-lite.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "tokenlean-lite.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "tokenlean-lite.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tokenlean-lite.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "tokenlean-lite.labels" -}}
helm.sh/chart: {{ include "tokenlean-lite.chart" . }}
{{ include "tokenlean-lite.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: tokenlean-lite
{{- end }}

{{- define "tokenlean-lite.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "tokenlean-lite.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* The proxy image, refused when it is not an exact release. */}}
{{- define "tokenlean-lite.image" -}}
{{- $tag := toString .Values.image.tag }}
{{- if or (eq $tag "") (eq $tag "latest") (eq $tag "set-me") }}
{{- fail "image.tag must name an exact proxy release (not empty, latest or set-me): pass --image to install.sh or --set image.tag=<release>" }}
{{- end }}
{{- printf "%s:%s" .Values.image.repository $tag }}
{{- end }}

{{/* The overlays the chart carries: repository path -> file in files/. */}}
{{- define "tokenlean-lite.knownOverlays" -}}
deploy/lite/config/langfuse.yaml: langfuse.yaml
{{- end }}

{{/* Checks that stop a render with a reason instead of deploying something unsafe. */}}
{{- define "tokenlean-lite.validate" -}}
{{- if ne .Values.config.profile "deploy/lite/config/agentic.yaml" }}
{{- fail "config.profile must be deploy/lite/config/agentic.yaml (the LITE profile the chart carries); add your changes as config.extraOverlays" }}
{{- end }}
{{- if ne .Values.proxyKeys.backend "postgres" }}
{{- fail "proxyKeys.backend must be postgres: the LITE profile keeps proxy keys in Postgres, so every replica sees a key issued or revoked through /admin/keys" }}
{{- end }}
{{- if not .Values.postgres.existingSecret }}
{{- fail "postgres.existingSecret must name the Secret that holds the Postgres password" }}
{{- end }}
{{- if not .Values.proxyKeys.existingSecret }}
{{- fail "proxyKeys.existingSecret must name the Secret that holds local-keys.json (the bootstrap keys, hashes only)" }}
{{- end }}
{{- if and (not .Values.redis.bundled.enabled) (not .Values.redis.host) }}
{{- fail "redis.host is empty: name your Redis, or set redis.bundled.enabled=true on a dev cluster" }}
{{- end }}
{{- $known := include "tokenlean-lite.knownOverlays" . | fromYaml }}
{{- range .Values.config.overlays }}
{{- if not (hasKey $known .) }}
{{- fail (printf "config.overlays: %s is not an overlay the chart carries (%s); put your own in config.extraOverlays" . (keys $known | sortAlpha | join ", ")) }}
{{- end }}
{{- end }}
{{- range .Values.config.extraOverlays }}
{{- if not (regexMatch "^[A-Za-z0-9][A-Za-z0-9_.-]*\\.ya?ml$" (toString .name)) }}
{{- fail (printf "config.extraOverlays: %q is not a file name ending in .yaml" (toString .name)) }}
{{- end }}
{{- if or (has .name (list "agentic.yaml" "config.yaml.template" "mock-provider.yaml")) (has .name (values $known)) }}
{{- fail (printf "config.extraOverlays: %s is a name the chart uses; choose another" .name) }}
{{- end }}
{{- end }}
{{- end }}

{{/* CONFIG_OVERLAYS: the profile, the carried overlays, the mock, then your own, in order. */}}
{{- define "tokenlean-lite.overlays" -}}
{{- $known := include "tokenlean-lite.knownOverlays" . | fromYaml }}
{{- $paths := list "/app/lite/config/agentic.yaml" }}
{{- range .Values.config.overlays }}
{{- $paths = append $paths (printf "/app/lite/config/%s" (get $known .)) }}
{{- end }}
{{- if .Values.mockProvider.enabled }}
{{- $paths = append $paths "/app/lite/config/mock-provider.yaml" }}
{{- end }}
{{- range .Values.config.extraOverlays }}
{{- $paths = append $paths (printf "/app/lite/config/%s" .name) }}
{{- end }}
{{- join "," $paths }}
{{- end }}

{{- define "tokenlean-lite.redisUrl" -}}
{{- if .Values.redis.bundled.enabled }}
{{- printf "redis://%s-redis:6379/0" (include "tokenlean-lite.fullname" .) }}
{{- else }}
{{- printf "%s://%s:%v/%v" (ternary "rediss" "redis" (.Values.redis.tls | default false)) .Values.redis.host .Values.redis.port .Values.redis.db }}
{{- end }}
{{- end }}

{{/* DATABASE_URL for <user>: no password in it (PGPASSWORD carries it). */}}
{{- define "tokenlean-lite.databaseUrl" -}}
{{- $p := .root.Values.postgres }}
{{- printf "postgresql://%s@%s:%v/%s?sslmode=%s" (urlquery .user) $p.host $p.port $p.database $p.sslmode }}
{{- end }}

{{- define "tokenlean-lite.podSecurity" -}}
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
automountServiceAccountToken: false
{{- end }}
