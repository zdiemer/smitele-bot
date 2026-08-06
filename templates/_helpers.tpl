{{- define "smitele-bot.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "smitele-bot.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "smitele-bot.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "smitele-bot.labels" -}}
helm.sh/chart: {{ include "smitele-bot.chart" . }}
{{ include "smitele-bot.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "smitele-bot.selectorLabels" -}}
app.kubernetes.io/name: {{ include "smitele-bot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Pull secrets: any externally-managed ones, plus the PAT-generated one if a PAT
is set. Emits nothing at all when there are none, so the caller can drop it in
unconditionally.
*/}}
{{- define "smitele-bot.imagePullSecrets" -}}
{{- if or .Values.imagePullSecrets .Values.imageCredentials.pat }}
imagePullSecrets:
{{- with .Values.imagePullSecrets }}
{{- toYaml . | nindent 2 }}
{{- end }}
{{- if .Values.imageCredentials.pat }}
  - name: {{ include "smitele-bot.fullname" . }}-ghcr
{{- end }}
{{- end }}
{{- end -}}

{{/*
Credentials, as environment variables. Shared verbatim by the bot Deployment
and the collector CronJob so the two can never drift apart.
*/}}
{{- define "smitele-bot.credentialEnv" -}}
- name: SMITELE_DISCORD_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "smitele-bot.fullname" . }}
      key: discordToken
- name: SMITELE_HIREZ_DEV_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "smitele-bot.fullname" . }}
      key: hirezDevId
- name: SMITELE_HIREZ_AUTH_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "smitele-bot.fullname" . }}
      key: hirezAuthKey
{{- end -}}

{{/*
The egress proxy, for the two workloads that talk to tracker.gg.

Kept out of credentialEnv deliberately. That helper exists so the three Hi-Rez
and Discord credentials can never drift apart, and it is included by six
workloads — but only the bot and the s2collector reach tracker.gg. Folding this
in would hand a metered proxy to the trainer and both aggregates for nothing,
and would make running the bot direct while the collector is proxied require a
duplicate-env override.

Emits nothing when unset, so a deployment that does not use a proxy renders
byte-for-byte as it did before this existed.
*/}}
{{- define "smitele-bot.egressEnv" -}}
{{- if .Values.credentials.egressProxy }}
- name: SMITELE_EGRESS_PROXY
  valueFrom:
    secretKeyRef:
      name: {{ include "smitele-bot.fullname" . }}
      key: egressProxy
      optional: true
{{- end }}
{{- end -}}
