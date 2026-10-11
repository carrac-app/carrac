package cc.convexity.widget

import android.content.Context
import android.util.Log
import androidx.glance.appwidget.updateAll
import androidx.work.*
import cc.convexity.widget.widgets.*
import kotlinx.coroutines.runBlocking
import java.util.concurrent.TimeUnit

class WidgetUpdateWorker(
    private val context: Context,
    params: WorkerParameters,
) : Worker(context, params) {

    override fun doWork(): Result {
        Log.d(TAG, "Widget update worker running")
        return try {
            val symbols = SecurePrefs.getSymbols(context)
            val data = ApiClient.fetchWidgetData(context, symbols)

            if (data != null) {
                runBlocking {
                    WatchlistWidget().updateAll(context)
                    PositionsWidget().updateAll(context)
                    SentimentWidget().updateAll(context)
                    AlertsWidget().updateAll(context)
                }
                Log.d(TAG, "Widget update complete — ${data.prices.size} prices")
                Result.success()
            } else {
                Log.w(TAG, "Widget update failed — no data")
                Result.retry()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Widget update worker crashed", e)
            Result.retry()
        }
    }

    companion object {
        private const val TAG = "WidgetUpdateWorker"
        private const val WORK_NAME = "convexity_widget_update"

        fun schedule(context: Context) {
            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()

            val request = PeriodicWorkRequestBuilder<WidgetUpdateWorker>(
                15, TimeUnit.MINUTES,
            )
                .setConstraints(constraints)
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 1, TimeUnit.MINUTES)
                .build()

            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                WORK_NAME, ExistingPeriodicWorkPolicy.UPDATE, request,
            )
            Log.d(TAG, "Widget update worker scheduled (15 min)")
        }
    }
}
