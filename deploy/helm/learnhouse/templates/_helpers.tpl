{{- define "learnhouse.config" -}}
LEARNHOUSE_DEVELOPMENT_MODE: "false"
LEARNHOUSE_TENANCY: "single"
LEARNHOUSE_DOMAIN: {{ .Values.domain | quote }}
LEARNHOUSE_FRONTEND_DOMAIN: {{ .Values.domain | quote }}
LEARNHOUSE_ALLOWED_ORIGINS: {{ printf "%s://%s" (ternary "https" "http" .Values.https) .Values.domain | quote }}
LEARNHOUSE_SSL: {{ .Values.https | toString | quote }}
LEARNHOUSE_AUTO_MIGRATE: "false"
LEARNHOUSE_AUTO_INSTALL: "false"
LEARNHOUSE_CONTENT_DELIVERY_TYPE: "s3api"
LEARNHOUSE_S3_API_REGION: {{ .Values.contentStorage.region | quote }}
AWS_DEFAULT_REGION: {{ .Values.contentStorage.region | quote }}
LEARNHOUSE_BIND_HOST: "0.0.0.0"
LEARNHOUSE_INTERNAL_API_URL: {{ printf "http://%s-api:9000" .Release.Name | quote }}
NEXT_PUBLIC_LEARNHOUSE_BACKEND_URL: {{ printf "%s://%s" (ternary "https" "http" .Values.https) .Values.domain | quote }}
NEXT_PUBLIC_LEARNHOUSE_DOMAIN: {{ .Values.domain | quote }}
NEXT_PUBLIC_LEARNHOUSE_TOP_DOMAIN: {{ .Values.domain | quote }}
NEXT_PUBLIC_LEARNHOUSE_HTTPS: {{ .Values.https | toString | quote }}
NEXT_PUBLIC_LEARNHOUSE_DEFAULT_ORG: "default"
{{- end -}}
