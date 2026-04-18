{{- if not .Values.persistence.existingClaim }}
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ include "windrose.pvcName" . }}
  namespace: {{ .Values.namespace }}
  labels:
{{ include "windrose.labels" . | indent 4 }}
spec:
  {{- if .Values.persistence.storageClassName }}
  storageClassName: {{ .Values.persistence.storageClassName | quote }}
  {{- end }}
  accessModes:
    - {{ .Values.persistence.accessMode }}
  resources:
    requests:
      storage: {{ .Values.persistence.size }}
{{- end }}
