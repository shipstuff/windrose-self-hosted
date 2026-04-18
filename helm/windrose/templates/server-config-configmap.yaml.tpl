{{- if and (eq .Values.serverConfig.mode "managed") .Values.serverConfig.inlineJson }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "windrose.managedConfigTemplateName" . }}
  namespace: {{ .Values.namespace }}
  labels:
{{ include "windrose.labels" . | indent 4 }}
data:
  ServerDescription.json: |
{{ .Values.serverConfig.inlineJson | toPrettyJson | indent 4 }}
{{- end }}
