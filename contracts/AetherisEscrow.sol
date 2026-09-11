// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 {
    function transferFrom(address sender, address recipient, uint256 amount) external returns (bool);
    function transfer(address recipient, uint256 amount) external returns (bool);
}

contract AetherisEscrow {
    address public immutable refereeOracle;
    address public immutable platformFeeWallet;
    IERC20 public immutable usdtToken;
    uint256 public constant PLATFORM_FEE_PERCENT = 5;

    struct EscrowMatch {
        uint256 totalPot;
        bool isSettled;
        bool exists;
    }

    mapping(bytes32 => EscrowMatch) public matches;

    event DepositLocked(bytes32 indexed matchId, address indexed player, uint256 amount);
    event SettlementExecuted(bytes32 indexed matchId, address indexed winner, uint256 payoutAmount, uint256 feeAmount);

    modifier onlyOracle() {
        require(msg.sender == refereeOracle, "AETHERIS: Caller is not authorized oracle");
        _;
    }

    constructor(address _usdtToken, address _platformFeeWallet) {
        refereeOracle = msg.sender;
        usdtToken = IERC20(_usdtToken);
        platformFeeWallet = _platformFeeWallet;
    }

    function depositStake(bytes32 matchId, uint256 amount) external {
        require(amount > 0, "AETHERIS: Invalid stake amount");
        require(!matches[matchId].isSettled, "AETHERIS: Match already finalized");

        require(usdtToken.transferFrom(msg.sender, address(this), amount), "AETHERIS: Transfer failed");

        if (!matches[matchId].exists) {
            matches[matchId] = EscrowMatch({totalPot: amount, isSettled: false, exists: true});
        } else {
            matches[matchId].totalPot += amount;
        }

        emit DepositLocked(matchId, msg.sender, amount);
    }

    function releaseWinnerPayout(bytes32 matchId, address winner) external onlyOracle {
        EscrowMatch storage currentMatch = matches[matchId];
        require(currentMatch.exists, "AETHERIS: Non-existent match");
        require(!currentMatch.isSettled, "AETHERIS: Escrow already settled");

        currentMatch.isSettled = true;

        uint256 feeAmount = (currentMatch.totalPot * PLATFORM_FEE_PERCENT) / 100;
        uint256 payoutAmount = currentMatch.totalPot - feeAmount;

        require(usdtToken.transfer(platformFeeWallet, feeAmount), "AETHERIS: Fee transfer failed");
        require(usdtToken.transfer(winner, payoutAmount), "AETHERIS: Payout transfer failed");

        emit SettlementExecuted(matchId, winner, payoutAmount, feeAmount);
    }
}
