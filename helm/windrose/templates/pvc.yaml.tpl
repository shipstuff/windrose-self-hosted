{{- include "windrose.validatePersistence" . -}}
{{- $root := . -}}
{{- $persistence := include "windrose.persistenceSpec" . | fromYaml -}}
{{- range $i, $claim := $persistence.claims }}
{{- if not $claim.existingClaim }}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ include "windrose.persistenceClaimName" (dict "root" $root "claim" $claim) }}
  namespace: {{ $root.Values.namespace }}
  labels:
{{ include "windrose.labels" $root | indent 4 }}
spec:
  {{- if $claim.storageClassName }}
  storageClassName: {{ $claim.storageClassName | quote }}
  {{- end }}
  accessModes:
    - {{ $claim.accessMode | default "ReadWriteOnce" }}
  resources:
    requests:
      storage: {{ $claim.size | default "20Gi" }}
{{- end }}
{{- end }}
