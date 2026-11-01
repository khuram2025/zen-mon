import { ChevronLeft, ChevronRight, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/Button'

export function isExplicitWizardSave(stepIndex: number, stepCount: number, pending: boolean, submitter: Element | null | undefined): boolean {
  return stepIndex === stepCount - 1 && !pending && submitter?.getAttribute('data-alert-save') === 'true'
}

export function AlertWizardFooter({ stepIndex, stepCount, editing, pending, onPrevious, onNext }: {
  stepIndex: number
  stepCount: number
  editing: boolean
  pending: boolean
  onPrevious: () => void
  onNext: () => void
}) {
  return (
    <div className="flex shrink-0 items-center justify-between border-t border-border px-5 py-4">
      <Button type="button" variant="outline" disabled={pending} onClick={onPrevious}>
        <ChevronLeft className="h-4 w-4" />{stepIndex > 0 ? 'Back' : 'Cancel'}
      </Button>
      <div className="flex gap-2">
        {stepIndex < stepCount - 1 ? (
          <Button key="next" type="button" disabled={pending} onClick={(event) => { event.preventDefault(); onNext() }}>
            Next<ChevronRight className="h-4 w-4" />
          </Button>
        ) : (
          <Button key="save" type="submit" data-alert-save="true" disabled={pending}>
            {pending && <Loader2 className="h-4 w-4 animate-spin" />}{editing ? 'Save alert' : 'Create alert'}
          </Button>
        )}
      </div>
    </div>
  )
}
