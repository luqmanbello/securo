import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import axios from 'axios'
import { connections } from '@/lib/api'
import { invalidateFinancialQueries } from '@/lib/invalidate-queries'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { toast } from 'sonner'

export interface CredentialField {
  name: string
  label_key: string
  placeholder_key?: string
  secret: boolean
}

const DEFAULT_FIELDS: CredentialField[] = [
  { name: 'user_id', label_key: 'accounts.credentialsConnect.userIdLabel', placeholder_key: 'accounts.credentialsConnect.userIdPlaceholder', secret: false },
  { name: 'password', label_key: 'accounts.credentialsConnect.passwordLabel', placeholder_key: 'accounts.credentialsConnect.passwordPlaceholder', secret: true },
]

interface CredentialsConnectDialogProps {
  open: boolean
  onClose: () => void
  provider: string
  reconnectConnectionId?: string
  fields?: CredentialField[]
}

export function CredentialsConnectDialog({
  open,
  onClose,
  provider,
  reconnectConnectionId,
  fields,
}: CredentialsConnectDialogProps) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const formFields = fields && fields.length > 0 ? fields : DEFAULT_FIELDS
  const [values, setValues] = useState<Record<string, string>>({})
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!open) {
      setValues({})
      setSubmitting(false)
    }
  }, [open])

  // Secrets are sent exactly as typed; everything else is trimmed.
  const cleaned = (f: CredentialField) => (f.secret ? values[f.name] ?? '' : (values[f.name] ?? '').trim())
  const complete = formFields.every((f) => cleaned(f) !== '')

  const i18nKey = `accounts.credentialsConnect.${provider}`
  const isReconnect = Boolean(reconnectConnectionId)

  const handleSubmit = async () => {
    if (!complete) return
    setSubmitting(true)
    try {
      await connections.handleCallback(
        JSON.stringify(Object.fromEntries(formFields.map((f) => [f.name, cleaned(f)]))),
        provider,
        undefined,
        undefined,
        reconnectConnectionId,
      )
      invalidateFinancialQueries(queryClient)
      queryClient.invalidateQueries({ queryKey: ['connections'] })
      toast.success(t(isReconnect ? 'accounts.reconnected' : 'accounts.connected'))
      onClose()
    } catch (err) {
      const detail =
        axios.isAxiosError(err) && err.response?.data?.detail
          ? typeof err.response.data.detail === 'string'
            ? err.response.data.detail
            : err.response.data.detail.message
          : null
      toast.error(detail || t('accounts.connectError'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && !submitting && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>
            {isReconnect
              ? t(`${i18nKey}.reconnectTitle`, t('accounts.credentialsConnect.reconnectTitle'))
              : t(`${i18nKey}.title`, t('accounts.credentialsConnect.defaultTitle'))}
          </DialogTitle>
          <p className="text-sm text-muted-foreground">
            {isReconnect
              ? t(`${i18nKey}.reconnectDescription`, t('accounts.credentialsConnect.reconnectDescription'))
              : t(`${i18nKey}.description`, t('accounts.credentialsConnect.defaultDescription'))}
          </p>
        </DialogHeader>

        <p className="text-xs text-muted-foreground">
          {t(`${i18nKey}.privacyNote`, t('accounts.credentialsConnect.privacyNote'))}
        </p>

        {formFields.map((f) => {
          const id = `securo-credentials-${f.name}`
          return (
            <div key={f.name} className="space-y-1.5">
              <label className="text-sm font-medium" htmlFor={id}>
                {t(f.label_key)}
              </label>
              <input
                id={id}
                type={f.secret ? 'password' : 'text'}
                className="w-full rounded-md border border-input bg-card px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-0"
                placeholder={f.placeholder_key ? t(f.placeholder_key) : undefined}
                value={values[f.name] ?? ''}
                onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                spellCheck={false}
                autoComplete={f.secret ? 'new-password' : 'off'}
                disabled={submitting}
              />
            </div>
          )
        })}

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={submitting}>
            {t('common.cancel')}
          </Button>
          <Button onClick={handleSubmit} disabled={!complete || submitting}>
            {submitting
              ? t('accounts.credentialsConnect.connecting')
              : t(isReconnect ? 'accounts.credentialsConnect.reconnect' : 'accounts.credentialsConnect.connect')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
