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

interface CredentialsConnectDialogProps {
  open: boolean
  onClose: () => void
  provider: string
  reconnectConnectionId?: string
}

export function CredentialsConnectDialog({
  open,
  onClose,
  provider,
  reconnectConnectionId,
}: CredentialsConnectDialogProps) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [userId, setUserId] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!open) {
      setUserId('')
      setPassword('')
      setSubmitting(false)
    }
  }, [open])

  const i18nKey = `accounts.credentialsConnect.${provider}`
  const isReconnect = Boolean(reconnectConnectionId)

  const handleSubmit = async () => {
    if (!userId.trim() || !password) return
    setSubmitting(true)
    try {
      await connections.handleCallback(
        JSON.stringify({ user_id: userId.trim(), password }),
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
          {t('accounts.credentialsConnect.privacyNote')}
        </p>

        <div className="space-y-1.5">
          <label className="text-sm font-medium" htmlFor="securo-credentials-user-id">
            {t('accounts.credentialsConnect.userIdLabel')}
          </label>
          <input
            id="securo-credentials-user-id"
            type="text"
            className="w-full rounded-md border border-input bg-card px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-0"
            placeholder={t('accounts.credentialsConnect.userIdPlaceholder')}
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            spellCheck={false}
            autoComplete="off"
            disabled={submitting}
          />
        </div>

        <div className="space-y-1.5">
          <label className="text-sm font-medium" htmlFor="securo-credentials-password">
            {t('accounts.credentialsConnect.passwordLabel')}
          </label>
          <input
            id="securo-credentials-password"
            type="password"
            className="w-full rounded-md border border-input bg-card px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-0"
            placeholder={t('accounts.credentialsConnect.passwordPlaceholder')}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            spellCheck={false}
            autoComplete="off"
            disabled={submitting}
          />
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={submitting}>
            {t('common.cancel')}
          </Button>
          <Button onClick={handleSubmit} disabled={!userId.trim() || !password || submitting}>
            {submitting
              ? t('accounts.credentialsConnect.connecting')
              : t(isReconnect ? 'accounts.credentialsConnect.reconnect' : 'accounts.credentialsConnect.connect')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
