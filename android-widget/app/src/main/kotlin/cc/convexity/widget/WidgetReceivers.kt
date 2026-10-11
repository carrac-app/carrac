package cc.convexity.widget

import android.appwidget.AppWidgetManager
import android.content.Context
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import cc.convexity.widget.widgets.*

/**
 * Base receiver that (re)arms the periodic update worker from widget
 * lifecycle events. Scheduling previously happened only in
 * WidgetConfigActivity — a widget added without re-running config never
 * got the 15-min worker, and a deferred/killed worker was never re-armed.
 * enqueueUniquePeriodicWork is idempotent, so calling on every onUpdate
 * (every 30 min via updatePeriodMillis) is safe and self-healing.
 */
abstract class BaseWidgetReceiver : GlanceAppWidgetReceiver() {

    override fun onEnabled(context: Context) {
        super.onEnabled(context)
        WidgetUpdateWorker.schedule(context)
        UpdateChecker.schedule(context)
    }

    override fun onUpdate(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray,
    ) {
        super.onUpdate(context, appWidgetManager, appWidgetIds)
        WidgetUpdateWorker.schedule(context)
    }
}

class WidgetReceivers {

    class WatchlistReceiver : BaseWidgetReceiver() {
        override val glanceAppWidget: GlanceAppWidget = WatchlistWidget()
    }

    class PositionsReceiver : BaseWidgetReceiver() {
        override val glanceAppWidget: GlanceAppWidget = PositionsWidget()
    }

    class SentimentReceiver : BaseWidgetReceiver() {
        override val glanceAppWidget: GlanceAppWidget = SentimentWidget()
    }

    class AlertsReceiver : BaseWidgetReceiver() {
        override val glanceAppWidget: GlanceAppWidget = AlertsWidget()
    }
}
