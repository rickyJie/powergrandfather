package com.csm.mobile

import androidx.lifecycle.MutableLiveData

/**
 * Broadcast bus for the tunnel service ↔ UI. Public LiveData so any
 * Activity can observe state transitions without service binding.
 */
enum class TunnelStatus {
    IDLE,
    CONNECTING,
    CONNECTED,
    RECONNECTING,
    ERROR,
    STOPPED,
}

data class TunnelStateSnapshot(
    val status: TunnelStatus,
    val message: String = "",
    val attempt: Int = 0,
)

object TunnelStateBus {
    val state = MutableLiveData(TunnelStateSnapshot(TunnelStatus.IDLE))

    fun post(status: TunnelStatus, message: String = "", attempt: Int = 0) {
        state.postValue(TunnelStateSnapshot(status, message, attempt))
    }
}
