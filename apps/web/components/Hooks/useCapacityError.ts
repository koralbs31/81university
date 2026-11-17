import { useTranslation } from 'react-i18next'
import { toast } from 'react-hot-toast'
import { isPlanLimitError } from '@services/utils/ts/errorMessage'

/** Report configured capacity errors without a payment or upgrade flow. */
export function useCapacityError() {
  const { t } = useTranslation()
  return {
    handleCapacityError: (res: any, _context?: { source?: string; feature?: string; requiredPlan?: string }) => {
      if (!isPlanLimitError(res?.data?.detail)) return false
      toast.error(t('common.capacity_reached'))
      return true
    },
  }
}
