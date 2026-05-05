{{- if .Values.metrics.grafanaDashboard.enabled }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "windrose.fullname" . }}-grafana-dashboard
  namespace: {{ .Values.metrics.grafanaDashboard.namespace | default .Values.namespace }}
  labels:
{{ include "windrose.labels" . | indent 4 }}
{{- with .Values.metrics.grafanaDashboard.labels }}
{{ toYaml . | indent 4 }}
{{- end }}
{{- with .Values.metrics.grafanaDashboard.annotations }}
  annotations:
{{ toYaml . | indent 4 }}
{{- end }}
data:
  windrose-overview.json: |-
{{ .Files.Get "dashboards/windrose-overview.json" | indent 4 }}
{{- end }}
