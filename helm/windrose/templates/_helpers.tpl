{{- define "windrose.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "windrose.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "windrose.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "windrose.labels" -}}
app.kubernetes.io/name: {{ include "windrose.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "windrose.persistenceClaimName" -}}
{{- $root := .root -}}
{{- $claim := .claim -}}
{{- if $claim.existingClaim -}}
{{- $claim.existingClaim -}}
{{- else -}}
{{- printf "%s-%s" (include "windrose.fullname" $root) $claim.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "windrose.reservedEnvNames" -}}
- WINDROSE_CONFIG_MODE
- WINDROSE_LAUNCH_STRATEGY
- SERVER_NAME
- INVITE_CODE
- IS_PASSWORD_PROTECTED
- MAX_PLAYER_COUNT
- P2P_PROXY_ADDRESS
- USE_DIRECT_CONNECTION
- DIRECT_CONNECTION_SERVER_ADDRESS
- DIRECT_CONNECTION_SERVER_PORT
- DIRECT_CONNECTION_PROXY_ADDRESS
- WORLD_ISLAND_ID
- WORLD_NAME
- WORLD_PRESET_TYPE
- PROTON_USE_XALIA
- DISABLE_SENTRY
- WINDROSE_PATCH_IDLE_CPU
- FILES_WAIT_TIMEOUT_SECONDS
- SERVER_LAUNCH_ARGS
- NET_SERVER_MAX_TICK_RATE
- WINDROSE_SERVER_SOURCE
- DISPLAY
- WINDROSE_MANAGED_CONFIG_TEMPLATE
- WINDROSE_MANAGED_CONFIG_PASSWORD_FILE
- EXTERNAL_CONFIG
- SERVER_PASSWORD
- UI_BIND
- UI_PORT
- UI_PASSWORD
- UI_ENABLE_ADMIN_WITHOUT_PASSWORD
- UI_SERVE_STATIC
- WINDROSE_WEBHOOK_EVENTS
- WINDROSE_WEBHOOK_POLL_SECONDS
- WINDROSE_WEBHOOK_TIMEOUT
- WINDROSE_WEBHOOK_URL
- WINDROSE_DISCORD_WEBHOOK_URL
- WINDROSE_GAME_CPU_LIMIT
- WINDROSE_GAME_MEM_LIMIT
{{- end -}}

{{- define "windrose.validateEnv" -}}
{{- $env := .Values.env | default list -}}
{{- if not (kindIs "slice" $env) -}}
{{- fail "env must be a list of Kubernetes EnvVar objects" -}}
{{- end -}}
{{- $reserved := include "windrose.reservedEnvNames" . | fromYamlArray -}}
{{- $seen := dict -}}
{{- range $i, $item := $env -}}
{{- $name := get $item "name" | default "" -}}
{{- if not $name -}}
{{- fail (printf "env[%d].name is required" $i) -}}
{{- end -}}
{{- if hasKey $seen $name -}}
{{- fail (printf "env contains duplicate name %q" $name) -}}
{{- end -}}
{{- $_ := set $seen $name true -}}
{{- if has $name $reserved -}}
{{- fail (printf "env[%d].name %q is managed by the chart; use the matching chart value instead of env[]" $i $name) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "windrose.validatePersistence" -}}
{{- $p := .Values.persistence | default dict -}}
{{- $legacyKeys := list "existingClaim" "size" "accessMode" "storageClassName" "subPath" -}}
{{- $hasLegacy := false -}}
{{- range $legacyKeys -}}
{{- if hasKey $p . -}}
{{- $hasLegacy = true -}}
{{- end -}}
{{- end -}}
{{- if $hasLegacy -}}
{{- $existingClaim := (get $p "existingClaim" | default "") -}}
{{- $size := (get $p "size" | default "20Gi") -}}
{{- $accessMode := (get $p "accessMode" | default "ReadWriteOnce") -}}
{{- $storageClassName := (get $p "storageClassName" | default "") -}}
{{- $subPath := (get $p "subPath" | default "steam-root") -}}
{{- $legacyIsDefault := and (eq $existingClaim "") (eq $size "20Gi") (eq $accessMode "ReadWriteOnce") (eq $storageClassName "") (eq $subPath "steam-root") -}}
{{- if not $legacyIsDefault -}}
{{- fail (printf `persistence.* scalar values were removed in this chart version.

Use the new format:

persistence:
  claims:
    - name: data
      existingClaim: %q
      size: %s
      accessMode: %s
      storageClassName: %q
  mounts:
    - name: data
      mountPath: /home/steam
      subPath: %s
` $existingClaim $size $accessMode $storageClassName $subPath) -}}
{{- end -}}
{{- end -}}
{{- if and (not (kindIs "slice" $p.claims)) (not $hasLegacy) -}}
{{- fail "persistence.claims must be a list of PVC claim definitions" -}}
{{- end -}}
{{- if and (not (kindIs "slice" $p.mounts)) (not $hasLegacy) -}}
{{- fail "persistence.mounts must be a list of volumeMount definitions" -}}
{{- end -}}
{{- $spec := include "windrose.persistenceSpec" . | fromYaml -}}
{{- $claims := dict -}}
{{- range $spec.claims -}}
{{- if not .name -}}
{{- fail "each persistence.claims[] entry must set name" -}}
{{- end -}}
{{- $_ := set $claims .name true -}}
{{- end -}}
{{- range $spec.mounts -}}
{{- if not .name -}}
{{- fail "each persistence.mounts[] entry must set name" -}}
{{- end -}}
{{- if not (hasKey $claims .name) -}}
{{- fail (printf "persistence.mounts[] references unknown claim name %q" .name) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "windrose.persistenceSpec" -}}
{{- $p := .Values.persistence | default dict -}}
{{- if kindIs "slice" $p.claims -}}
claims:
{{ toYaml $p.claims | indent 2 }}
mounts:
{{ toYaml $p.mounts | indent 2 }}
{{- else -}}
{{- $existingClaim := (get $p "existingClaim" | default "") -}}
{{- $size := (get $p "size" | default "20Gi") -}}
{{- $accessMode := (get $p "accessMode" | default "ReadWriteOnce") -}}
{{- $storageClassName := (get $p "storageClassName" | default "") -}}
{{- $subPath := (get $p "subPath" | default "steam-root") -}}
claims:
  - name: data
    existingClaim: {{ $existingClaim | quote }}
    size: {{ $size }}
    accessMode: {{ $accessMode }}
    storageClassName: {{ $storageClassName | quote }}
mounts:
  - name: data
    mountPath: /home/steam
    subPath: {{ $subPath | quote }}
{{- end -}}
{{- end -}}

{{- define "windrose.managedConfigTemplateName" -}}
{{- printf "%s-managed-config" (include "windrose.fullname" .) -}}
{{- end -}}
